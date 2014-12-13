# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# Getting Things Gnome! - a personal organizer for the GNOME desktop
# Copyright (c) 2008-2009 - Lionel Dricot & Bertrand Rousseau
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.
# -----------------------------------------------------------------------------

""" Google Tasks backend

Task reference: https://developers.google.com/google-apps/tasks/v1/reference/tasks#resource

Icon for this backend is part of google-tasks-chrome-extension (
http://code.google.com/p/google-tasks-chrome-extension/ ) published under
the terms of Apache License 2.0"""

import os
import httplib2
import uuid
import datetime
import webbrowser
import threading

from GTG import _
from GTG.backends.backendsignals import BackendSignals
from GTG.backends.genericbackend import GenericBackend
from GTG.backends.periodicimportbackend import PeriodicImportBackend
from GTG.backends.syncengine import SyncEngine, SyncMeme
from GTG.core import CoreConfig
from GTG.core.task import Task
from GTG.tools.interruptible import interruptible
from GTG.tools.logger import Log
from GTG.tools.dates import Date

# External libraries
from apiclient.discovery import build as build_service
from oauth2client.client import FlowExchangeError
from oauth2client.client import OAuth2WebServerFlow
from oauth2client.file import Storage


class Backend(PeriodicImportBackend):
    # Credence for authorizing GTG as an app
    CLIENT_ID = '94851023623.apps.googleusercontent.com'
    CLIENT_SECRET = 'p6H1UGaDLAJjDaoUbwu0lNJz'

    _general_description = {
        GenericBackend.BACKEND_NAME: "backend_gtask",
        GenericBackend.BACKEND_HUMAN_NAME: _("Google Tasks"),
        GenericBackend.BACKEND_AUTHORS: ["Madhumitha Viswanathan",
                                         "Izidor Matušov",
                                         "Luca Invernizzi"],
        GenericBackend.BACKEND_TYPE: GenericBackend.TYPE_READWRITE,
        GenericBackend.BACKEND_DESCRIPTION:
            _("Synchronize your GTG tasks with Google Tasks \n\n"
              "Legal note: This product uses the Google Tasks API but is not "
              "endorsed or certified by Google Tasks"),
        }

    _static_parameters = {
        "period": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_INT,
            GenericBackend.PARAM_DEFAULT_VALUE: 5, },
        "is-first-run": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_BOOL,
            GenericBackend.PARAM_DEFAULT_VALUE: True, },
        }

    def __init__(self, parameters):
        '''
        See GenericBackend for an explanation of this function.
        Re-loads the saved state of the synchronization
        '''
        super(Backend, self).__init__(parameters)
        self.storage = None
        self.service = None
        self.authenticated = False
        #loading the list of already imported tasks
        self.data_path = os.path.join('backends/gtask/', "tasks_dict-%s" %
                                      self.get_id())
        self.sync_engine = self._load_pickled_file(self.data_path,
                                                   SyncEngine())

    def save_state(self):
        '''
        See GenericBackend for an explanation of this function.
        Saves the state of the synchronization.
        '''
        self._store_pickled_file(self.data_path, self.sync_engine)

    def initialize(self):
        """
        Intialize backend: try to authenticate. If it fails, request an authorization.
        """
        super(Backend, self).initialize()
        path = os.path.join(CoreConfig().get_data_dir(), 'backends/gtask',
                            'storage_file-%s' % self.get_id())
        # Try to create leading directories that path
        path_dir = os.path.dirname(path)
        if not os.path.isdir(path_dir):
            os.makedirs(path_dir)

        self.storage = Storage(path)
        self.authenticate()

    def authenticate(self):
        """ Try to authenticate by already existing credences or request an authorization """
        self.authenticated = False

        credentials = self.storage.get()
        if credentials is None or credentials.invalid:
            self.request_authorization()
        else:
            self.apply_credentials(credentials)

    def apply_credentials(self, credentials):
        """ Finish authentication or request for an authorization by applying the credentials """
        http = httplib2.Http()
        http = credentials.authorize(http)

        # Build a service object for interacting with the API.
        self.service = build_service(serviceName='tasks', version='v1', http=http,
                                     developerKey='AIzaSyAmUlk8_iv-rYDEcJ2NyeC_KVPNkrsGcqU')

        self.authenticated = True

    def _authorization_step2(self, code):
        credential = self.flow.step2_exchange(code)

        self.storage.put(credential)
        credential.set_store(self.storage)

        return credential

    def request_authorization(self):
        """ Make the first step of authorization and open URL for allowing the access """
        self.flow = OAuth2WebServerFlow(client_id=self.CLIENT_ID,
                                        client_secret=self.CLIENT_SECRET,
                                        scope='https://www.googleapis.com/auth/tasks',
                                        user_agent='GTG')

        oauth_callback = 'oob'
        url = self.flow.step1_get_authorize_url(oauth_callback)
        browser_thread = threading.Thread(target=lambda: webbrowser.open_new(url))
        browser_thread.daemon = True
        browser_thread.start()

        # Request the code from user
        BackendSignals().interaction_requested(self.get_id(), _(
            "You need to <b>authorize GTG</b> to access your tasks on <b>Google</b>.\n"
            "<b>Check your browser</b>, and follow the steps there.\n"
            "When you are done, press 'Continue'."),
            BackendSignals().INTERACTION_TEXT,
            "on_authentication_step")

    def on_authentication_step(self, step_type="", code=""):
        """ First time return specification of dialog.
            The second time grab the code and make the second, last
            step of authorization, afterwards apply the new credentials """

        if step_type == "get_ui_dialog_text":
            return _("Code request"), _("Paste the code Google has given you"
                                        "here")
        elif step_type == "set_text":
            try:
                credentials = self._authorization_step2(code)
            except FlowExchangeError:
                # Show an error to user and end
                self.quit(disable=True)
                BackendSignals().backend_failed(self.get_id(),
                                                BackendSignals.ERRNO_AUTHENTICATION)
                return

            self.apply_credentials(credentials)
            # Request periodic import, avoid waiting a long time
            self.start_get_tasks()

    def get_tasklist(self, task):
        '''
        Returns the tasklist id of a given task

        @param task: the id of the Google Task
        '''
        # Wait until authentication
        if not self.authenticated:
            return
        #Loop through all the tasklists
        tasklists = self.service.tasklists().list().execute()

        for taskslist in tasklists['items']:
            #Loop through all the tasks of a tasklist
            gtasklist = self.service.tasks().list(tasklist=taskslist['id']).execute()

            if 'items' not in gtasklist.keys():
                return

            for gtask in gtasklist['items']:
                if gtask['id'] != task:
                    return taskslist['id']

        Log.warn('No match found for ' + gtask['title'] + ' - ' + gtask['id'])

    def do_periodic_import(self):
        # Wait until authentication
        if not self.authenticated:
            return
        #get all the tasklists
        tasklists = self.service.tasklists().list().execute()
        for taskslist in tasklists['items']:
            gtasklist = self.service.tasks().list(tasklist=taskslist['id']).execute()

            if 'items' not in gtasklist.keys():
                return

            for gtask in gtasklist['items']:
                if 'tasklist' not in gtask:
                    continue
                self._process_gtask(gtask['id'])
                self.get_tasklist(gtask['id'])

            gtask_ids = [gtask['id'] for gtask in gtasklist['items']]
            stored_task_ids = self.sync_engine.get_all_remote()
            for gtask in set(stored_task_ids).difference(set(gtask_ids)):
                self.on_gtask_deleted(gtask, None)

    @interruptible
    def on_gtask_deleted(self, gtask, something):
        '''
        Callback, executed when a Google Task is deleted.
        Deletes the related GTG task.

        @param gtask: the id of the Google Task
        @param something: not used, here for signal callback compatibility
        '''
        with self.datastore.get_backend_mutex():
            self.cancellation_point()
            try:
                tid = self.sync_engine.get_local_id(gtask)
            except KeyError:
                return
            if self.datastore.has_task(tid):
                self.datastore.request_task_deletion(tid)
                self.break_relationship(remote_id=gtask)

    @interruptible
    def remove_task(self, tid):
        '''
        See GenericBackend for an explanation of this function.

        @param gtasklist: a Google tasklist id
        '''
        with self.datastore.get_backend_mutex():
            self.cancellation_point()
            try:
                gtask = self.sync_engine.get_remote_id(tid)
            except KeyError:
                return
            #the remote task might have been already deleted manually (or by
            #another app)
            try:
                self.service.tasks().delete(tasklist=self.gtasklist, task=gtask).execute()
            except Exception as e:
                #FIXME:need to see if disconnected...
                Log.error(e)
            self.break_relationship(local_id=tid)

    def _process_gtask(self, gtask):
        '''
        Given a Google Task id, finds out if it must be synced to a GTG note and,
        if so, it carries out the synchronization (by creating or updating a GTG
        task, or deleting itself if the related task has been deleted)

        @param gtask: a Google Task id
        @param gtasklist: a Google tasklist id
        '''
        with self.datastore.get_backend_mutex():
            self.cancellation_point()
            is_syncable = self._google_task_is_syncable(gtask)
            action, tid = self.sync_engine.analyze_remote_id(
                gtask, self.datastore.has_task, self._google_task_exists(gtask), is_syncable)

            if action == SyncEngine.ADD:
                tid = str(uuid.uuid4())
                task = self.datastore.task_factory(tid)
                self._populate_task(task, gtask)
                self.record_relationship(
                    local_id=tid, remote_id=gtask,
                    meme=SyncMeme(task.get_modified(),
                                  self.get_modified_for_task(gtask),
                                  self.get_id()))
                self.datastore.push_task(task)

            elif action == SyncEngine.REMOVE:
                self.service.tasks().delete(tasklist=self.get_tasklist(gtask), task=gtask).execute()
                self.break_relationship(local_id=tid)
                try:
                    self.sync_engine.break_relationship(remote_id=gtask)
                except KeyError:
                    pass

            elif action == SyncEngine.UPDATE:
                task = self.datastore.get_task(tid)
                meme = self.sync_engine.get_meme_from_remote_id(gtask)
                newest = meme.which_is_newest(
                    task.get_modified(), self.get_modified_for_task(gtask))
                if newest == "remote":
                    self._populate_task(task, gtask)
                    meme.set_local_last_modified(task.get_modified())
                    meme.set_remote_last_modified(self.get_modified_for_task(gtask))
                    self.save_state()

            elif action == SyncEngine.LOST_SYNCABILITY:
                self._exec_lost_syncability(tid, gtask)

    @interruptible
    def set_task(self, task):
        '''
        See GenericBackend for an explanation of this function.

        '''
        # Skip if not authenticated
        if not self.authenticated:
            return

        self.cancellation_point()
        is_syncable = self._gtg_task_is_syncable_per_attached_tags(task)
        tid = task.get_id()
        with self.datastore.get_backend_mutex():
            action, gtask_id = self.sync_engine.analyze_local_id(
                tid, self.datastore.has_task(tid),
                self._google_task_exists, is_syncable)
            Log.debug("processing gtg (%s, %d)" % (action, is_syncable))
            if action == SyncEngine.ADD:
                gtask = {'title': ' '}
                gtask_id = self._populate_gtask(gtask, task)
                self.record_relationship(
                    local_id=tid, remote_id=gtask_id,
                    meme=SyncMeme(task.get_modified(),
                                  self.get_modified_for_task(gtask_id),
                                  "GTG"))

            elif action == SyncEngine.REMOVE:
                self.datastore.request_task_deletion(tid)
                try:
                    self.sync_engine.break_relationship(local_id=tid)
                    self.save_state()
                except KeyError:
                    pass

            elif action == SyncEngine.UPDATE:
                meme = self.sync_engine.get_meme_from_local_id(task.get_id())
                newest = meme.which_is_newest(
                    task.get_modified(), self.get_modified_for_task(gtask_id))
                if newest == "local":
                    gtask = self.service.tasks().get(tasklist='@default', task=gtask_id).execute()
                    self._update_gtask(gtask, task)
                    meme.set_local_last_modified(task.get_modified())
                    meme.set_remote_last_modified(self.get_modified_for_task(gtask_id))
                    self.save_state()

            elif action == SyncEngine.LOST_SYNCABILITY:
                pass
                #self._exec_lost_syncability(tid, note)

###############################################################################
### Helper methods ############################################################
###############################################################################

    @interruptible
    def on_gtask_saved(self, gtask):
        '''
        Callback, executed when a Google task is saved by Google Tasks itself
        Updates the related GTG task (or creates one, if necessary).

        @param gtask: the id of the Google Taskk
        '''
        self.cancellation_point()

        @interruptible
        def _execute_on_gtask_saved(self, gtask):
            self.cancellation_point()
            self._process_gtask(gtask)
            self.save_state()

    def _google_task_is_syncable(self, gtask):
        '''
        Returns True if this Google Task should be synced into GTG tasks.

        @param gtask: the google task id
        @returns Boolean
        '''
        return True

    def _google_task_exists(self, gtask):
        '''
        Returns True if  a calendar exists with the given id.

        @param gtask: the Google Task id
        @returns Boolean
        @param gtasklist: a Google tasklist id
        '''
        try:
            self.service.tasks().get(tasklist=self.get_tasklist(gtask), task=gtask).execute()
            return True
        except:
            return False

    def get_modified_for_task(self, gtask):
        '''
        Returns the modification time for the given google task id.

        @param gtask: the google task id
        @returns datetime.datetime
        @param gtasklist: a Google tasklist id
        '''

        #try:
        gtask_instance = self.service.tasks().get(tasklist=self.get_tasklist(gtask), task=gtask).execute()
        #except:
         #   pass
        modified_time = datetime.datetime.strptime(gtask_instance['updated'], "%Y-%m-%dT%H:%M:%S.%fZ" )
        return modified_time

    def _populate_task(self, task, gtask):
        '''
        Copies the content of a Google task into a GTG task.

        @param task: a GTG Task
        @param gtask: a Google Task id
        @param gtasklist: a Google tasklist id
        '''
        gtask_instance = self.service.tasks().get(tasklist=self.get_tasklist(gtask), task=gtask).execute()
        text = ' '  # gtask_instance['notes']
        if text is None:
            text = ' '
        #update the tags list
        #task.set_only_these_tags(extract_tags_from_text(text))
        title = gtask_instance['title']
        task.set_title(title)
        task.set_text(text)

        # Status: If the task is active in Google, mark it as active in GTG.
        #         If the task is completed in Google, in GTG it can be either
        #           dismissed or done.
        gtg_status = task.get_status()
        google_status = gtask_instance['status']
        if google_status == "needsAction":
            task.set_status(Task.STA_ACTIVE)
        elif google_status == "completed" and gtg_status == Task.STA_ACTIVE:
            task.set_status(Task.STA_DONE)

        #FIXME set_due_date to be added
        task.add_remote_id(self.get_id(), gtask)

    def _populate_gtask(self, gtask, task):
        '''
        Copies the content of a task into a Google Task.

        @param gtask: a Google Task
        @param task: a GTG Task
        '''
        title = task.get_title()
        content = task.get_excerpt(strip_subtasks=False)

        gtask = {
            'title': title,
            'notes': content,
        }

        #start_time = task.get_start_date().to_py_date().strftime('%Y-%m-%dT%H:%M:%S.000Z' )
        due = task.get_due_date()
        if due != Date.no_date():
            gtask['due'] = due.date().strftime('%Y-%m-%dT%H:%M:%S.000Z')

        result = self.service.tasks().insert(tasklist=self.get_tasklist(gtask), body=gtask).execute()

        if task.get_status() == Task.STA_ACTIVE:
            gtask['status'] = "needsAction"
        else:
            gtask['status'] = "completed"
        return result['id']

    def _update_gtask(self, gtask, task):
        '''
        Updates the content of a Google task if some change is made in the GTG Task.

        @param gtask: a Google Task
        @param task: a GTG Task
        '''
        title = task.get_title()
        content = task.get_excerpt(strip_subtasks=False)

        #start_time = task.get_start_date().to_py_date().strftime('%Y-%m-%dT%H:%M:%S.000Z' )
        due = task.get_due_date()
        if due != Date.no_date():
            gtask['due'] = due.date().strftime('%Y-%m-%dT%H:%M:%S.000Z')

        gtask['title'] = title
        gtask['notes'] = content
        self.service.tasks().update(
            tasklist=self.get_tasklist(gtask['id']), task=gtask['id'], body=gtask
        ).execute()

    def _exec_lost_syncability(self, tid, gtask):
        '''
        Executed when a relationship between tasks loses its syncability
        property. See SyncEngine for an explanation of that.
        This function finds out which object (task/note) is the original one
        and which is the copy, and deletes the copy.

        @param tid: a GTG task tid
        @param gtask: a Google task id
        '''
        self.cancellation_point()
        meme = self.sync_engine.get_meme_from_remote_id(gtask)
        #First of all, the relationship is lost
        self.sync_engine.break_relationship(remote_id=gtask)
        if meme.get_origin() == "GTG":
            self.service.tasks().delete(tasklist=self.get_tasklist(gtask), task=gtask).execute()
        else:
            self.datastore.request_task_deletion(tid)

    def break_relationship(self, *args, **kwargs):
        '''
        Proxy method for SyncEngine.break_relationship, which also saves the
        state of the synchronization.
        '''
        try:
            self.sync_engine.break_relationship(*args, **kwargs)
            #we try to save the state at each change in the sync_engine:
            #it's slower, but it should avoid widespread task
            #duplication
            self.save_state()
        except KeyError:
            pass

    def record_relationship(self, *args, **kwargs):
        '''
        Proxy method for SyncEngine.break_relationship, which also saves the
        state of the synchronization.
        '''

        self.sync_engine.record_relationship(*args, **kwargs)
        #we try to save the state at each change in the sync_engine:
        #it's slower, but it should avoid widespread task
        #duplication
        self.save_state()
