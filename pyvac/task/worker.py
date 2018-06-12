# -*- coding: utf-8 -*-
import sys
import yaml
import json

from datetime import datetime
import logging
import transaction
try:
    from collections import OrderedDict
except ImportError:
    OrderedDict = dict

from celery.task import Task, subtask

from pyvac.models import DBSession, Request, User, Reminder, RequestHistory
from pyvac.helpers.calendar import addToCal
from pyvac.helpers.mail import SmtpCache
from pyvac.helpers.conf import ConfCache

try:
    from yaml import CSafeLoader as YAMLLoader
except ImportError:
    from yaml import SafeLoader as YAMLLoader

log = logging.getLogger(__name__)


class BaseWorker(Task):

    def process(self, data):
        raise NotImplementedError

    def run(self, *args, **kwargs):
        self.log = log
        self.session = DBSession()
        self.smtp = SmtpCache()
        self.log.debug('using session %r, %r' %
                       (self.session, id(self.session)))

        req = kwargs.get('data')
        # don't log credentials like in caldav.url
        params = req.copy()
        params.pop('caldav.url', None)
        self.log.info('[Task %s]: RECEIVED %r' % (self.name, params))

        self.process(req)

        return True

    def send_mail(self, sender, target, request, content):
        """ Send a mail """
        subject = 'Request %s (%s)' % (request.status, request.user.name)
        self.smtp.send_mail(sender, target, subject, content)

    def send_mail_ics(self, sender, target, request, content):
        """ Send a multipart mail with ics file as attachment """
        subject = 'Request %s (%s)' % (request.status, request.user.name)
        ics_content = request.generate_vcal_entry()
        self.smtp.send_mail_multipart(sender, target, subject, content,
                                      newpart=ics_content)

    def send_mail_custom(self, subject, sender, target, content):
        """ Send a mail """
        self.smtp.send_mail(sender, target, subject, content)

    def get_admin_mail(self, admin):
        """ Return admin email from ldap dict or model """
        if isinstance(admin, dict):
            return admin['email']
        else:
            return admin.email


class WorkerPending(BaseWorker):

    name = 'worker_pending'

    def process(self, data):
        """ submitted by user
        send mail to manager
        """
        req = Request.by_id(self.session, data['req_id'])
        # send mail to manager
        src = req.user.email
        dst = req.user.manager_mail
        if 'reminder' in data:
            content = """A request from %s is still waiting your approval
Request details: %s""" % (req.user.name, req.summarymail)
        else:
            content = """New request from %s
Request details: %s""" % (req.user.name, req.summarymail)
        try:
            self.send_mail(sender=src, target=dst, request=req,
                           content=content)
            # update request status after sending email
            req.notified = True
        except Exception as err:
            self.log.exception('Error while sending mail')
            req.flag_error(str(err), self.session)

        self.session.flush()
        transaction.commit()


class WorkerPendingNotified(BaseWorker):

    name = 'worker_pending_notified'

    def process(self, data):
        """ submitted by user

        re-send mail to manager if close to requested date_from
        """
        req = Request.by_id(self.session, data['req_id'])
        # after new field was added, it may not be set yet
        if not req.date_updated:
            return

        delta_deadline = req.date_from - req.date_updated
        if delta_deadline.days <= 2:
            if datetime.now().date() != req.date_updated.date():
                # resend the mail
                self.log.info('2 days left before requested date, '
                              'remind the manager')

                data['reminder'] = True
                async_result = subtask(WorkerPending).delay(data=data)
                self.log.info('task scheduled %r' % async_result)


class WorkerAccepted(BaseWorker):

    name = 'worker_accepted'

    def process(self, data):
        """ accepted by manager
        send mail to user
        send mail to HR
        """
        req = Request.by_id(self.session, data['req_id'])
        # send mail to user
        src = req.user.manager_mail
        dst = req.user.email
        content = """Your request has been accepted by %s. Waiting for HR validation.
Request details: %s""" % (req.user.manager_name, req.summarymail)
        try:
            self.send_mail(sender=src, target=dst, request=req,
                           content=content)

            # send mail to HR
            # if multiple admins in a BU, send a mail to each one.
            admins = req.user.get_admin(self.session, full=True)
            for admin in admins:
                dst = self.get_admin_mail(admin)
                content = """Manager %s has accepted a new request. Waiting for your validation.
    Request details: %s""" % (req.user.manager_name, req.summarymail)
                self.send_mail(sender=src, target=dst, request=req,
                               content=content)

            # update request status after sending email
            req.notified = True
        except Exception as err:
            self.log.exception('Error while sending mail')
            req.flag_error(str(err), self.session)

        self.session.flush()
        transaction.commit()


class WorkerAcceptedNotified(BaseWorker):

    name = 'worker_accepted_notified'

    def process(self, data):
        """ accepted by manager
        auto flag as accepted by HR
        """
        req = Request.by_id(self.session, data['req_id'])
        # after new field was added, it may not be set yet
        if not req.date_updated:
            return

        delta = datetime.now() - req.date_updated
        # after Request.date_updated + 3 days, auto accept it by HR
        if delta.days >= 3:
            # auto accept it as HR
            self.log.info('3 days passed, auto accept it by HR')

            # create history entry
            msg = 'Automatically accepted by HR after 3 days passed'
            # use error_message field, as it should not be used here
            # if it fails in ERROR it should be overwritten anyway
            # as the status will be changed from APPROVED_ADMIN to ERROR
            RequestHistory.new(self.session, req,
                               req.status, 'APPROVED_ADMIN',
                               user=None, error_message=msg)
            # update request status after sending email
            req.update_status('APPROVED_ADMIN')
            self.session.flush()
            transaction.commit()

            data['autoaccept'] = True
            async_result = subtask(WorkerApproved).delay(data=data)
            self.log.info('task scheduled %r' % async_result)


class WorkerApproved(BaseWorker):

    name = 'worker_approved'

    def process(self, data):
        """ approved by HR
        send mail to user
        send mail to manager
        """
        req = Request.by_id(self.session, data['req_id'])

        # retrieve admin
        admin = req.user.get_admin(self.session)
        req_admin = req.get_admin(self.session)
        if req_admin:
            admin = req_admin

        # send mail to user
        src = self.get_admin_mail(admin)
        dst = req.user.email
        if 'autoaccept' in data:
            content = """Your request was automatically approved, it has been added to calendar.
Request details: %s""" % req.summarymail
        else:
            content = """HR has accepted your request, it has been added to calendar.
Request details: %s

You can find the corresponding .ics file as attachment.""" % req.summarymail
        try:
            self.send_mail_ics(sender=src, target=dst, request=req,
                               content=content)

            # send mail to manager
            src = self.get_admin_mail(admin)
            dst = req.user.manager_mail
            if 'autoaccept' in data:
                content = """A request you accepted was automatically approved, it has been added to calendar.
Request details: %s""" % req.summarymail # noqa
            else:
                content = """HR has approved a request you accepted, it has been added to calendar.
Request details: %s""" % req.summarymail
            self.send_mail(sender=src, target=dst, request=req,
                           content=content)

            # update request status after sending email
            req.notified = True
        except Exception as err:
            self.log.exception('Error while sending mail')
            req.flag_error(str(err), self.session)

        try:
            if 'caldav.url' in data:
                caldav_url = data['caldav.url']
            else:
                conf_file = sys.argv[1]
                with open(conf_file) as fdesc:
                    Conf = yaml.load(fdesc, YAMLLoader) # noqa
                caldav_url = Conf.get('caldav').get('url')

            # add new entry in caldav
            ics_url = addToCal(caldav_url,
                               req.date_from,
                               req.date_to,
                               req.summarycal)
            # save ics url in request
            req.ics_url = ics_url
            self.log.info('Request %d added to cal: %s ' % (req.id, ics_url))
        except Exception as err:
            self.log.exception('Error while adding to calendar')
            req.flag_error(str(err), self.session)

        self.session.flush()
        transaction.commit()


class WorkerDenied(BaseWorker):

    name = 'worker_denied'

    def process(self, data):
        """ denied by last_action_user_id
        send mail to user
        """
        req = Request.by_id(self.session, data['req_id'])

        # retrieve user who performed last action
        action_user = User.by_id(self.session, req.last_action_user_id)
        # send mail to user
        src = action_user.email
        dst = req.user.email
        content = """Your request has been refused for the following reason: %s
Request details: %s""" % (req.reason, req.summarymail)
        try:
            self.send_mail(sender=src, target=dst, request=req,
                           content=content)

            # update request status after sending email
            req.notified = True
        except Exception as err:
            self.log.exception('Error while sending mail')
            req.flag_error(str(err), self.session)

        self.session.flush()
        transaction.commit()


class WorkerMail(BaseWorker):

    name = 'worker_mail'

    def process(self, data):
        """ simple worker task for sending a mail using internal helper """

        sender = data['sender']
        target = data['target']
        subject = data['subject']
        content = data['content']

        try:
            self.smtp.send_mail(sender, target, subject, content)
        except Exception:
            self.log.exception('Error while sending mail')


class WorkerTrialReminder(BaseWorker):

    name = 'worker_trial_reminder'

    def process(self, data):
        """send a trial reminder mail for a user to his admin"""
        user = User.by_id(self.session, data['user_id'])
        duration = data['duration']

        conf = ConfCache()
        sender = conf.get('reminder', {}).get('sender', 'pyvac')
        # send mail to user country admin (HR)
        subject = 'Trial period reminder: %s' % user.name
        admin = user.get_admin(self.session)
        target = self.get_admin_mail(admin)
        content = """Hello,

This is a reminder that %s trial period has been running for %d months.
Arrival date: %s

""" % (user.name, duration, user.arrival_date.strftime('%d/%m/%Y'))

        try:
            self.smtp.send_mail(sender, target, subject, content)

            data = {'user_id': user.id}
            parameters = json.dumps(OrderedDict(data))
            rem = Reminder(type='trial_threshold', parameters=parameters)
            self.session.add(rem)
            self.session.flush()
            transaction.commit()

        except Exception:
            self.log.exception('Error while sending mail')
