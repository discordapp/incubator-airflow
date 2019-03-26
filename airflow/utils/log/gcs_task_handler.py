# -*- coding: utf-8 -*-
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import os
import time
from google.cloud import storage

from airflow import configuration
from airflow.exceptions import AirflowException
from airflow.utils.log.logging_mixin import LoggingMixin
from airflow.utils.log.file_task_handler import FileTaskHandler


class GCSTaskHandler(FileTaskHandler, LoggingMixin):
    """
    GCSTaskHandler is a python log handler that handles and reads
    task instance logs. It extends airflow FileTaskHandler and
    uploads to and reads from GCS remote storage. Upon log reading
    failure, it reads from host machine's local disk.
    """
    def __init__(self, base_log_folder, gcs_log_folder, filename_template):
        super(GCSTaskHandler, self).__init__(base_log_folder, filename_template)
        self.remote_base = gcs_log_folder
        self.log_relative_path = ''
        self._hook = None
        self.closed = False
        self.upload_on_close = True
        self._gcs_client = None

    def _build_hook(self):
        remote_conn_id = configuration.conf.get('core', 'REMOTE_LOG_CONN_ID')
        try:
            from airflow.contrib.hooks.gcs_hook import GoogleCloudStorageHook
            return GoogleCloudStorageHook(
                google_cloud_storage_conn_id=remote_conn_id
            )
        except Exception as e:
            self.log.error(
                'Could not create a GoogleCloudStorageHook with connection id '
                '"%s". %s\n\nPlease make sure that airflow[gcp] is installed '
                'and the GCS connection exists.', remote_conn_id, str(e)
            )

    @property
    def hook(self):
        if self._hook is None:
            self._hook = self._build_hook()
        return self._hook

    @property
    def gcs_client(self):
        if self._gcs_client is None:
            self._gcs_client = storage.Client()
        return self._gcs_client

    def set_context(self, ti):
        super(GCSTaskHandler, self).set_context(ti)
        # Log relative path is used to construct local and remote
        # log path to upload log files into GCS and read from the
        # remote location.
        self.log_relative_path = self._render_filename(ti, ti.try_number)
        self.upload_on_close = not ti.raw

    def close(self):
        """
        Close and upload local log file to remote storage S3.
        """
        # When application exit, system shuts down all handlers by
        # calling close method. Here we check if logger is already
        # closed to prevent uploading the log to remote storage multiple
        # times when `logging.shutdown` is called.
        if self.closed:
            return

        super(GCSTaskHandler, self).close()

        if not self.upload_on_close:
            return

        local_loc = os.path.join(self.local_base, self.log_relative_path)
        remote_loc = os.path.join(self.remote_base, self.log_relative_path)
        if os.path.exists(local_loc):
            # read log and remove old logs to get just the latest additions
            with open(local_loc, 'r') as logfile:
                log = logfile.read()
            self.gcs_write(log, remote_loc)

        # Mark closed so we don't double write if close is called twice
        self.closed = True

    def _read(self, ti, try_number, metadata=None):
        """
        Read logs of given task instance and try_number from GCS.
        If failed, read the log from task instance host machine.
        :param ti: task instance object
        :param try_number: task instance try_number to read logs from
        :param metadata: log metadata,
                         can be used for steaming log reading and auto-tailing.
        """
        # Explicitly getting log relative path is necessary as the given
        # task instance might be different than task instance passed in
        # in set_context method.
        log_relative_path = self._render_filename(ti, try_number)
        remote_loc = os.path.join(self.remote_base, log_relative_path)

        try:
            remote_log = self.gcs_read(remote_loc)
            log = '*** Reading remote log from {}.\n{}\n'.format(
                remote_loc, remote_log)
            return log, {'end_of_log': True}
        except Exception as e:
            log = '*** Unable to read remote log from {}\n*** {}\n\n'.format(
                remote_loc, str(e))
            self.log.error(log)
            local_log, metadata = super(GCSTaskHandler, self)._read(ti, try_number)
            log += local_log
            return log, metadata

    def gcs_read(self, remote_log_location):
        """
        Returns the log found at the remote_log_location.
        :param remote_log_location: the log's location in remote storage
        :type remote_log_location: str (path)
        """
        bkt, blob = self.parse_gcs_url(remote_log_location)
        return self.gcs_download_with_retries(bkt, blob).decode('utf-8')

    def gcs_download_with_retries(self, bucket, blob, max_tries=5, try_number=1):
        """
        Retry download operation with exponential backoff (up to 1 minute of wait time).
        :param bucket: GCS bucket
        :param blob: GCS blob
        :param max_tries: Maximum number of retries before giving up and throwing
        :param try_number: The current try number
        :return: The content of the blob at the GCS bucket/blob location. This will throw
        an Exception if the hook fails after max_tries attempts.
        """
        if try_number > max_tries:
            raise Exception(f"Unable to perform download hook after {max_tries} attempts.")

        try:
            if self.hook.download(bucket, blob):
                if try_number > 1:
                    self.log.info(f'Succeeded after {try_number} attempts.')
                return

        except Exception as e:
            self.log.error(f'Failed to download log to gcs with exception {e}')

        self.log.warn('Failed to perform download hook; trying again')

        time.sleep(min(60., 0.01 * (1 << try_number)))
        self.gcs_download_with_retries(bucket, blob, max_tries, try_number + 1)

    def gcs_upload_with_retries(self, bucket, blob, tmpfile_name, max_tries=5, try_number=1):
        """
        Retry upload operation with exponential backoff (up to 1 minute of wait time).
        :param bucket:  GCS bucket
        :param blob:  GCS blob
        :param tmpfile_name: Local tempfile
        :param max_tries: Maximum number of retries before giving up and throwing
        :param try_number: The current try number
        :return: True if the upload is successful. This will throw if the hook is
        not able to fulfill the upload request after max_tries attempts.
        """
        if try_number > max_tries:
            raise Exception(f"Unable to perform upload hook after {max_tries} attempts.")

        try:
            if self.hook.upload(bucket, blob, tmpfile_name):
                if try_number > 1:
                    self.log.info(f'Succeeded after {try_number} attempts.')
                return True

        except Exception as e:
            self.log.error(f'Failed to upload log to gcs with exception {e}')

        self.log.warn('Failed to perform upload hook; trying again')

        time.sleep(min(60., 0.01 * (1 << try_number)))
        self.gcs_upload_with_retries(bucket, blob, tmpfile_name, max_tries, try_number + 1)

    def gcs_client_upload_with_retries(self, bucket_name, blob_name, log, max_tries=5, try_number = 1):
        """
        Retry upload operation with exponential backoff (up to 1 minute of wait time).
        :param bucket:  GCS bucket
        :param blob:  GCS blob
        :param log: Log payload to upload
        :param max_tries: Maximum number of retries before giving up and throwing
        :param try_number: The current try number
        :return: True if the upload is successful. This will throw if the hook is
        not able to fulfill the upload request after max_tries attempts.
        """
        if try_number > max_tries:
            raise Exception(f"Unable to perform gcs upload after {max_tries} attempts.")

        try:
            blob = self.gcs_client.bucket(bucket_name).blob(blob_name)
            blob.upload_from_string(log)

        except Exception as e:
            self.log.error(f'Failed to upload log with gcs_client to gcs with exception {e}')

        self.log.warn('Failed to perform upload hook; trying again')

        time.sleep(min(60., 0.01 * (1 << try_number)))
        self.gcs_upload_with_retries(bucket_name, blob_name, log, max_tries, try_number + 1)

    def gcs_write(self, log, remote_log_location, append=True):
        """
        Writes the log to the remote_log_location. Fails silently if no hook
        was created.
        :param log: the log to write to the remote_log_location
        :type log: str
        :param remote_log_location: the log's location in remote storage
        :type remote_log_location: str (path)
        :param append: if False, any existing log file is overwritten. If True,
            the new log is appended to any existing logs.
        :type append: bool
        """
        if append:
            try:
                old_log = self.gcs_read(remote_log_location)
                log = '\n'.join([old_log, log]) if old_log else log
            except Exception as e:
                if not hasattr(e, 'resp') or e.resp.get('status') != '404':
                    log = '*** Previous log discarded: {}\n\n'.format(str(e)) + log

        try:
            bkt, blob = self.parse_gcs_url(remote_log_location)
        except Exception as e:
            self.log.error(f'Could not parse {remote_log_location}', e)
        else:
            try:
                from tempfile import NamedTemporaryFile
                with NamedTemporaryFile(mode='w+') as tmpfile:
                    tmpfile.write(log)
                    # Force the file to be flushed, since we're doing the
                    # upload from within the file context (it hasn't been
                    # closed).
                    tmpfile.flush()
                    self.gcs_upload_with_retries(bkt, blob, tmpfile.name)
            except Exception as e:
                self.log.error('Could not write logs to %s: %s', remote_log_location, e)

                ### try again with plain-old gcs client
                self.gcs_client_upload_with_retries(bkt, blob, log)

    @staticmethod
    def parse_gcs_url(gsurl):
        """
        Given a Google Cloud Storage URL (gs://<bucket>/<blob>), returns a
        tuple containing the corresponding bucket and blob.
        """
        # Python 3
        try:
            from urllib.parse import urlparse
        # Python 2
        except ImportError:
            from urlparse import urlparse

        parsed_url = urlparse(gsurl)
        if not parsed_url.netloc:
            raise AirflowException('Please provide a bucket name')
        else:
            bucket = parsed_url.netloc
            blob = parsed_url.path.strip('/')
            return bucket, blob
