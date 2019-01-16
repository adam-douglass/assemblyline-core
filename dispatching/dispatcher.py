import time
import datetime
import json
import uuid
import traceback
from functools import reduce

# TODO replace with unique queue
from assemblyline.remote.datatypes.queues.named import NamedQueue
from assemblyline.remote.datatypes.hash import Hash, ExpiringHash, ExpiringSet
from assemblyline.remote.datatypes import counter
from assemblyline.common import isotime, net, forge

from dispatch_hash import DispatchHash
from configuration import Scheduler, CachedObject, config_hash
from assemblyline import odm
from assemblyline.odm.models.submission import Submission
import watcher


def service_queue_name(service):
    return 'service-queue-'+service


def create_missing_file_error(submission, sha):
    odm.models.error.Error({
        'created': datetime.datetime.utcnow(),
        'response': {
            'message': f'Submission {submission.sid} tried to process missing file.',
            'service_debug_info': traceback.format_stack(),
            'service_name': 'dispatcher',
            'service_version': '4',
            'status': 'FAIL_NONRECOVERABLE',
        },
        'sha256': sha
    })


@odm.model()
class FileTask(odm.Model):
    sid = odm.Keyword()
    parent_hash = odm.KeyWord()
    file_hash = odm.Keyword()
    file_type = odm.Keyword()
    depth = odm.Integer()


@odm.model()
class ServiceTask(odm.Model):
    sid = odm.Keyword()
    file_hash = odm.Keyword()
    file_type = odm.Keyword()
    depth = odm.Integer()
    service_name = odm.Keyword()
    service_config = odm.Keyword()



FILE_QUEUE = 'dispatch-file'
SUBMISSION_QUEUE = 'submission'


class Dispatcher:

    def __init__(self, datastore, redis, redis_persist, logger):
        # Load the datastore collections that we are going to be using
        self.ds = datastore
        self.log = logger
        self.submissions = datastore.submissions
        self.results = datastore.results
        self.errors = datastore.errors
        self.files = datastore.files

        # Create a config cache that will refresh config values periodically
        self.config = CachedObject(forge.get_config)
        self.scheduler = Scheduler(datastore, self.config)

        # Connect to all of our persistant redis structures
        self.redis = redis
        self.redis_persist = redis_persist
        self.submission_queue = NamedQueue(SUBMISSION_QUEUE, redis)
        self.file_queue = NamedQueue(FILE_QUEUE, redis)

        # Publish counters to the metrics sink.
        self.counts = counter.AutoExportingCounters(
            name='dispatcher-%s' % self.shard,
            host=net.get_hostname(),
            auto_flush=True,
            auto_log=False,
            export_interval_secs=self.config.system.update_interval,
            channel=forge.get_metrics_sink(),
            counter_type='dispatcher')

    def start(self):
        self.counts.start()

        # self.service_manager.start()

        # This starts a thread that polls for messages with an exponential
        # backoff, if no messages are found, to a maximum of one second.
        # minimum = -6
        # maximum = 0
        # self.running = True
        #
        # threading.Thread(target=self.heartbeat).start()
        # for _ in range(8):
        #     threading.Thread(target=self.writer).start()
        #
        # signal.signal(signal.SIGINT, self.interrupt)
        #
        # time.sleep(2 * int(config.system.update_interval))

        # exp = minimum
        # while self.running:
        #     if self.poll(len(self.entries)):
        #         exp = minimum
        #         continue
        #     if self.drain and not self.entries:
        #         break
        #     time.sleep(2**exp)
        #     exp = exp + 1 if exp < maximum else exp
        #     self.check_timeouts()
        #
        # counts.stop()

    def dispatch_submission(self, submission: Submission):
        """
        Find any files associated with a submission and dispatch them if they are
        not marked as in progress. If all files are finished, finalize the submission.

        Preconditions:
            - File exists in the filestore and file collection in the datastore
            - Submission is stored in the datastore
        """
        sid = submission.sid

        # Refresh the watch, this ensures that this function will be called again
        # if something goes wrong with one of the files, and it never finishes.
        watcher.touch(self.redis, key=sid, timeout=self.config.core.dispatcher.timeout,
                      queue=SUBMISSION_QUEUE, message={'sid': sid})

        # Refresh the quota hold
        if submission.params.quota_item and submission.params.submitter:
            self.log.info(f"Submission {sid} counts toward quota for {submission.params.submitter}")
            Hash('submissions-' + submission.params.submitter, self.redis_persist).add(sid, isotime.now_as_iso())

        # Open up the file/service table for this submission
        process_table = DispatchHash(submission.sid, self.redis)
        depth_limit = self.config.core.dispatcher.extraction_depth_limit

        # Try to find all files, and extracted files
        unchecked_files = []
        for file_hash in submission.files:
            file_data = self.files.get(file_hash.sha256)
            if not file_data:
                self.errors.save(uuid.uuid4().hex, create_missing_file_error(submission, file_hash.sha256))
                continue

            unchecked_files.append(FileTask(dict(
                sid=sid,
                file_hash=file_hash,
                file_type=file_data.type,
                depth=0
            )))
        encountered_files = {file.sha256 for file in submission.files}
        pending_files = {}

        # Track information about the results as we hit them
        max_score = None
        result_classifications = []

        # For each file, we will look through all its results, any exctracted files
        # found
        while unchecked_files:
            task = unchecked_files.pop()
            sha = task.file_hash
            schedule = self.scheduler.build_schedule(submission, task.file_type)

            for service_name in reduce(lambda a, b: a + b, schedule):
                # If the service is still marked as 'in progress'
                runtime = time.time() - process_table.dispatch_time(sha, service_name)
                if runtime < self.scheduler.service_timeout(service_name):
                    pending_files[sha] = task
                    continue

                # It hasn't started, has timed out, or is finished, see if we have a result
                result_key = process_table.finished(sha, service_name)

                # No result found, mark the file as incomplete
                if not result_key:
                    pending_files[sha] = task
                    continue

                # The process table is marked that a service has been abandoned due to errors
                if result_key == 'errors':
                    continue

                # If we have hit the depth limit, ignore children
                if task.depth >= depth_limit:
                    continue

                # The result should exist then, get all the sub-files
                result = self.results.get(result_key)
                for sub_file in result.extracted_files:
                    if sub_file not in encountered_files:
                        encountered_files.add(sub_file)

                        file_type = self.files.get(file_hash).type
                        unchecked_files.append(FileTask(dict(
                            sid=sid,
                            file_hash=sub_file,
                            file_type=file_type,
                            depth=task.depth + 1
                        )))

                # Collect information about the result
                if max_score is None:
                    max_score = result.score
                else:
                    max_score = max(max_score, result.score)
                result_classifications.append(result.classification)

        # If there are pending files, then at least one service, on at least one
        # file isn't done yet, poke those files
        if pending_files:
            for task in pending_files.values():
                self.file_queue.push(task)
        else:
            self.finalize_submission(submission, result_classifications, max_score)

    def finalize_submission(self, submission: Submission, result_classifications, max_score):
        """All of the services for all of the files in this submission have finished or failed.

        Update the records in the datastore, and flush the working data from redis.
        """
        sid = submission.sid

        # Erase tags
        ExpiringSet(task.get_tag_set_name()).delete()
        ExpiringHash(task.get_submission_tags_name()).delete()

        if submission.params.quota_item and submission.params.submitter:
            self.log.info(f"Submission {sid} no longer counts toward quota for {submission.params.submitter}")
            Hash('submissions-' + submission.params.submitter, self.redis_persist).pop(sid)

        # All the  remove this sid as well.
        # entries.pop(sid)
        process_table = DispatchHash(submission.sid, self.redis)
        results = process_table.all_results()
        process_table.delete()

        # Pull in the classifications of ???
        c12ns = dispatcher.completed.pop(sid).values() # TODO where are these classifications coming from
        classification = Classification.UNRESTRICTED
        for c12n in c12ns:
            classification = Classification.max_classification(
                classification, c12n
            )

        # TODO should we reverse the pointer here and start adding
        # a SID to the error object?
        errors = []  # dispatcher.errors.pop(sid, [])

        # submission['original_classification'] = submission['classification']
        submission.classification = classification
        submission.error_count = len(errors)
        submission.errors = errors
        submission.file_count = len(set([x[:64] for x in errors + results]))
        submission['results'] = results
        submission.max_score = max_score
        submission.state = 'completed'
        submission.times.completed = isotime.now_as_iso()
        self.submissions.sive(sid, submission)

        completed_queue = submission.params.completed_queue
        if completed_queue:
            raw = submission.json()
            raw.update({
                'errors': errors,
                'results': results,
                'error_count': len(set([x[:64] for x in errors])),
                'file_count': len(set([x[:64] for x in errors + results])),
            })

            self.open_queue(completed_queue).push(raw)

        # Send complete message to any watchers.
        for w in dispatcher.watchers.pop(sid, {}).itervalues():
            w.push({'status': 'STOP'})

    def dispatch_file(self, task: FileTask):
        """ Handle a message describing a file to be processed.

        This file may be:
            - A new submission or extracted file.
            - A file that has just completed a stage of processing.
            - A file that has not completed a a stage of processing, but this
              call has been triggered by a timeout or similar.

        If the file is totally new, we will setup a dispatch table, and fill it in.

        Once we make/load a dispatch table, we will dispatch whichever group the table
        shows us hasn't been completed yet.

        When we dispatch to a service, we check if the task is already in the dispatch
        queue. If it isn't proceed normally. If it is, check that the service is still online.
        """
        # Read the message content
        file_hash = task.file_hash
        submission = self.submissions.get(task.sid)
        now = time.time()

        # Refresh the watch on the submission, we are still working on it
        watcher.touch(self.redis, key=task.sid, timeout=self.config.core.dispatcher.timeout,
                      queue=SUBMISSION_QUEUE, message={'sid': task.sid})

        # Open up the file/service table for this submission
        process_table = DispatchHash(task.sid, *self.redis)

        # Calculate the schedule for the file
        schedule = self.scheduler.build_schedule(submission, task.file_type)

        # Go through each round of the schedule removing complete/failed services
        # Break when we find a stage that still needs processing
        outstanding = {}
        tasks_remaining = 0
        while schedule and not outstanding:
            stage = schedule.pop(0)

            for service in stage:
                # If the result is in the process table we are fine
                if process_table.finished(file_hash, service):
                    if not submission.params.ignore_filtering and process_table.dropped(file_hash, service):
                        schedule.clear()
                    continue

                # queue_size = self.queue_size[name] = self.queue_size.get(name, 0) + 1
                # entry.retries[name] = entry.retries.get(name, -1) + 1

                # if task.profile:
                #     if entry.retries[name]:
                #         log.info('%s Graph: "%s" -> "%s/%s" [label=%d];',
                #         sid, srl, srl, name, entry.retries[name])
                #     else:
                #         log.info('%s Graph: "%s" -> "%s/%s";',
                #         sid, srl, srl, name)
                #         log.info('%s Graph: "%s/%s" [label=%s];',
                #         sid, srl, name, name)

                file_count = len(self.entries[sid]) + len(self.completed[sid])

                # Warning: Please do not change the text of the error messages below.
                msg = None
                if self._service_is_down(service, now):
                    log.debug(' '.join((msg, "Not sending %s/%s to %s." % (sid, srl, name))))
                    response = Task(deepcopy(task.raw))
                    response.watermark(name, '')
                    response.nonrecoverable_failure(msg)
                    self.storage_queue.push({
                    'type': 'error',
                    'name': name,
                    'response': response,
                    })
                    return False

                if service.skip(task):
                    response = Task(deepcopy(task.raw))
                    response.watermark(name, '')
                    response.success()
                    q.send_raw(response.as_dispatcher_response())
                    return False

                    task.sent = now
                    service.proxy.execute(task.priority, task.as_service_request(name))

                # Check if something, an error/a result already exists, to resolve this service
                config = self.scheduler.build_service_config(service, submission)
                access_key = self._find_results(submission, file_hash, service, config)
                if access_key:
                    result = self.results.get(access_key)
                    if result:
                        drop = result.drop_file
                        tasks_remaining = process_table.finish(file_hash, service, access_key, drop=drop)
                        if not submission.params.ignore_filtering and drop:
                            schedule.clear()
                    continue

                outstanding[service] = config

        # Try to retry/dispatch any outstanding services
        if outstanding:
            for service, config in outstanding.items():
                # Check if this submission has already dispatched this service, and hasn't timed out yet
                queued_time = time.time() - process_table.dispatch_time(file_hash, service)
                if queued_time < self.scheduler.service_timeout(service):
                    continue

                # Build the actual service dispatch message
                service_task = ServiceTask(dict(
                    service_name=service,
                    service_config=json.dumps(config),
                    **task.as_primitives()
                ))

                queue = NamedQueue(service_queue_name(service), *self.redis)
                queue.push(service_task.as_primitives())
                process_table.dispatch(file_hash, service)

        else:
            # dispatcher.storage_queue.push({
            #     'type': 'complete',
            #     'expiry': task.__expiry_ts__,
            #     'filescore_key': task.scan_key,
            #     'now': now,
            #     'psid': task.psid,
            #     'score': int(dispatcher.score.get(sid, None) or 0),
            #     'sid': sid,
            # })
            #
            #             key = msg['filescore_key']
            #             if key:
            #                 store.save_filescore(
            #                     key, msg['expiry'], {
            #                         'psid': msg['psid'],
            #                         'sid': msg['sid'],
            #                         'score': msg['score'],
            #                         'time': msg['now'],
            #                     }
            #                 )

            # There are no outstanding services, this file is done

            # If there are no outstanding ANYTHING for this submission,
            # send a message to the submission dispatcher to finalize
            self.counts.increment('dispatch.files_completed')
            if process_table.all_finished() and tasks_remaining == 0:
                self.submission_queue.push({'sid': submission.sid})

    def _find_results(self, sid, file_hash, service, config):
        """
        Try to find any results or terminal errors that satisfy the
        request to run `service` on `file_hash` for the configuration in `submission`
        """
        # Look for results that match this submission/hash/service config
        key = self.scheduler.build_result_key(file_hash, service, config_hash(config))
        if self.results.exists(key):
            # TODO Touch result expiry
            return key

        # NOTE these searches can be parallel
        # NOTE these searches need to be changed to match whatever the error log is set to
        # Search the errors for one matching this submission/hash/service
        # that also has a terminal flag
        results = self.errors.search(f"sid:{sid} AND file_hash:{file_hash} AND service:{service} "
                                     "AND catagory:'terminal'", rows=1, fl=[self.ds.ID])
        for result in results['items']:
            return result.id

        # Count the crash or timeout errors for submission/hash/service
        results = self.errors.search(f"sid:{sid} AND file_hash:{file_hash} AND service:{service} "
                                     "AND (catagory:'timeout' OR catagory:'crash')", rows=0)
        if results['total'] > self.scheduler.service_failure_limit(service):
            return 'errors'

        # No reasons not to continue processing this file
        return False

    # def heartbeat(self):
    #     while not self.drain:
    #         with self.lock:
    #             heartbeat = {
    #                 'shard': self.shard,
    #                 'entries': len(self.entries),
    #                 'errors': len(self.errors),
    #                 'results': len(self.results),
    #                 'resources': {
    #                     "cpu_usage.percent": psutil.cpu_percent(),
    #                     "mem_usage.percent": psutil.virtual_memory().percent,
    #                     "disk_usage.percent": psutil.disk_usage('/').percent,
    #                     "disk_usage.free": psutil.disk_usage('/').free,
    #                 },
    #                 'services': self._service_info(), 'queues': {
    #                     'max_inflight': self.high,
    #                     'control': self.control_queue.length(),
    #                     'ingest': q.length(self.ingest_queue),
    #                     'response': q.length(self.response_queue),
    #                 },
    #                 'hostinfo': self.hostinfo
    #             }
    #
    #             msg = message.Message(to="*", sender='dispatcher', mtype=message.MT_DISPHEARTBEAT, body=heartbeat)
    #             CommsQueue('status').publish(msg.as_dict())
    #
    #         time.sleep(1)
    #
    # # noinspection PyUnusedLocal
    # def interrupt(self, unused1, unused2):  # pylint: disable=W0613
    #     if self.drain:
    #         log.info('Forced shutdown.')
    #         self.running = False
    #         return
    #
    #     log.info('Shutting down gracefully...')
    #     # Rename control queue to 'control-<hostname>-<pid>-<seconds>-<shard>'.
    #     self.control_queue = \
    #         forge.get_control_queue('control-' + self.response_queue)
    #     self.drain = True