import os
import base64
import logging
import pickle

import uuid

import import_string

from datetime import timedelta

from django.core.management import call_command, get_commands
from django.core.exceptions import ImproperlyConfigured
from django.core.cache import caches
from django.db import close_old_connections, transaction
from django.db.utils import InterfaceError, OperationalError
from django.utils.timezone import now

try:
    from celery import Task, shared_task, current_app
    from celery.result import AsyncResult
    from celery.exceptions import CeleryError, TimeoutError
    from kombu.utils import uuid as task_uuid
    from kombu import serialization
except ImportError:
    raise ImproperlyConfigured('Missing celery library, please install it')

from .config import settings


logger = logging.getLogger(__name__)


cache = caches[settings.CACHE_NAME]


def default_unique_key_generator(task, task_args, task_kwargs):
    task_args = task_args or ()
    task_kwargs = task_kwargs or {}

    _, _, data = serialization.dumps(
        (list(task_args), task_kwargs), task._get_app().conf.task_serializer,
    )
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, ':'.join((settings.KEY_PREFIX, task.name, data))))


class NotTriggeredCeleryError(CeleryError):
    pass


class OnCommitAsyncResult:

    def __init__(self):
        self._result = None

    def set_result(self, result):
        self._result = result

    def get(self, *args, **kwargs):
        if self._result is None:
            raise NotTriggeredCeleryError('Celery task has not been triggered yet')
        else:
            return self._result.get(*args, **kwargs)

    @property
    def state(self):
        if self._result is None:
            return 'WAITING'
        else:
            return self._result.state

    def successful(self):
        if self._result is None:
            return False
        else:
            return self._result.successful()

    def failed(self):
        if self._result is None:
            return False
        else:
            return self._result.failed()

    @property
    def task_id(self):
        if self._result is None:
            return None
        else:
            return self._result.task_id


class AsyncResultWrapper:

    def __init__(self, invocation_id, result, task, args, kwargs, options):
        self._invocation_id = invocation_id
        self._result = result
        self._task = task
        self._args = args
        self._kwargs = kwargs
        self._options = options

    def set_result(self, result):
        self._result = result

    def get(self, *args, **kwargs):
        try:
            return self._result.get()
        except TimeoutError as ex:
            self.timeout(ex)
            raise ex

    def timeout(self, ex):
        self._task.on_invocation_timeout(self._invocation_id, self._args, self._kwargs, self.task_id, ex, self._options)

    @property
    def state(self):
        return self._result.state

    def successful(self):
        return self._result.successful()

    def failed(self):
        return self._result.failed()

    @property
    def task_id(self):
        return self._result.task_id

    @property
    def id(self):
        return self._result.id


class DjangoTask(Task):

    abstract = True

    # Support set retry delay in list. Retry countdown value is get from list where index is attempt
    # number (request.retries)
    default_retry_delays = None
    # Unique task if task with same input already exists no extra task is created and old task result is returned
    unique = False
    unique_key_generator = default_unique_key_generator
    _stackprotected = True

    @property
    def max_queue_waiting_time(self):
        return settings.DEFAULT_TASK_MAX_QUEUE_WAITING_TIME

    @property
    def stale_time_limit(self):
        return settings.DEFAULT_TASK_STALE_TIME_LIMIT

    def on_invocation_apply(self, invocation_id, args, kwargs, options):
        """
        Method is called when task was applied with the requester.
        :param invocation_id: UUID of the requester invocation
        :param args: input task args
        :param kwargs: input task kwargs
        :param options: input task options
        """
        pass

    def on_invocation_trigger(self, invocation_id, args, kwargs, task_id, options):
        """
        Task has been triggered and placed in the queue.
        :param invocation_id: UUID of the requester invocation
        :param args: input task args
        :param kwargs: input task kwargs
        :param task_id: UUID of the celery task
        :param options: input task options
        """
        pass

    def on_invocation_unique(self, invocation_id, args, kwargs, task_id, options):
        """
        Task has been triggered but the same task is already active.
        Therefore only pointer to the active task is returned.
        :param invocation_id: UUID of the requester invocation
        :param args: input task args
        :param kwargs: input task kwargs
        :param task_id: UUID of the celery task
        :param options: input task options
        """
        pass

    def on_invocation_timeout(self, invocation_id, args, kwargs, task_id, ex, options):
        """
        Task has been joined to another unique async result.
        :param invocation_id: UUID of the requester invocation
        :param args: input task args
        :param kwargs: input task kwargs
        :param task_id: UUID of the celery task
        :param ex: celery TimeoutError
        :param options: input task options
        """
        pass

    def on_task_start(self, task_id, args, kwargs):
        """
        Task has been started with worker.
        :param task_id: UUID of the celery task
        :param args: input task args
        :param kwargs: input task kwargs
        """
        pass

    def on_task_retry(self, task_id, args, kwargs, exc, eta):
        """
        Task failed but will be retried.
        :param task_id: UUID of the celery task
        :param args: task args
        :param kwargs: task kwargs
        :param exc: raised exception which caused retry
        :param eta: time to next retry
        """
        pass

    def on_task_failure(self, task_id, args, kwargs, exc, einfo):
        """
        Task failed and will not be retried.
        :param task_id: UUID of the celery task
        :param args: task args
        :param kwargs: task kwargs
        :param exc: raised exception
        :param einfo: exception traceback
        """
        pass

    def on_task_success(self, task_id, args, kwargs, retval):
        """
        Task was successful.
        :param task_id: UUID of the celery task
        :param args: task args
        :param kwargs: task kwargs
        :param retval: task result
        """
        pass

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        super().on_failure(exc, task_id, args, kwargs, einfo)
        self.on_task_failure(task_id, args, kwargs, exc, einfo)
        self._clear_unique_key(args, kwargs)

    def on_success(self, retval, task_id, args, kwargs):
        super().on_success(retval, task_id, args, kwargs)
        self.on_task_success(task_id, args, kwargs, retval)
        self._clear_unique_key(args, kwargs)

    def __call__(self, *args, **kwargs):
        """
        Overrides parent which works with thread stack. We didn't want to allow change context which was generated in
        one of apply methods. Call task directly is now disallowed.
        """
        req = self.request_stack.top

        if not req or req.called_directly:
            raise CeleryError(
                'Task cannot be called directly. Please use apply, apply_async or apply_async_on_commit methods'
            )

        if req._protected:
            raise CeleryError('Request is protected')
        # request is protected (no usage in celery but get from function _install_stack_protection in
        # celery library)
        req._protected = 1

        # Every set attr is sent here
        self.on_task_start(req.id, args, kwargs)
        return self._start(*args, **kwargs)

    def _start(self, *args, **kwargs):
        return self.run(*args, **kwargs)

    def _get_unique_key(self, task_args, task_kwargs):
        return self.unique_key_generator(task_args, task_kwargs) if self.unique else None

    def _clear_unique_key(self, task_args, task_kwarg):
        unique_key = self._get_unique_key(task_args, task_kwarg)
        if unique_key:
            cache.delete(unique_key)

    def _get_unique_task_id(self, unique_key, task_id, stale_time_limit):
        if unique_key and not stale_time_limit:
            raise CeleryError('For unique tasks is require set task stale_time_limit')

        if unique_key and not self._get_app().conf.task_always_eager:
            if cache.add(unique_key, task_id, stale_time_limit):
                return task_id
            else:
                unique_task_id = cache.get(unique_key)
                return (
                    unique_task_id if unique_task_id
                    else self._get_unique_task_id(unique_key, task_id, stale_time_limit)
                )
        else:
            return task_id

    def _compute_eta(self, eta, countdown, trigger_time):
        if countdown is not None:
            return trigger_time + timedelta(seconds=countdown)
        elif eta:
            return eta
        else:
            return trigger_time

    def _compute_expires(self, expires, time_limit, stale_time_limit, trigger_time):
        expires = self.expires if expires is None else expires
        if expires is not None:
            return trigger_time + timedelta(seconds=expires) if isinstance(expires, int) else expires
        elif stale_time_limit is not None and time_limit is not None:
            return trigger_time + timedelta(seconds=stale_time_limit - time_limit)
        else:
            return None

    def _get_time_limit(self, time_limit):
        if time_limit is not None:
            return time_limit
        elif self.soft_time_limit is not None:
            return self.soft_time_limit
        else:
            return self._get_app().conf.task_time_limit

    def _get_stale_time_limit(self, expires, time_limit, stale_time_limit, trigger_time):
        if stale_time_limit is not None:
            return stale_time_limit
        elif self.stale_time_limit is not None:
            return self.stale_time_limit
        elif time_limit is not None and self.max_queue_waiting_time:
            autoretry_for = getattr(self, 'autoretry_for', None)
            if autoretry_for and self.default_retry_delays:
                return (
                    (time_limit + self.max_queue_waiting_time) * len(self.default_retry_delays) + 1
                    + sum(self.default_retry_delays)
                )
            elif autoretry_for:
                return (
                    (time_limit + self.max_queue_waiting_time + self.default_retry_delay) * self.max_retries
                    + time_limit + self.max_queue_waiting_time
                )
            else:
                return time_limit + self.max_queue_waiting_time
        else:
            return None

    def _apply_and_get_wrapped_result(self, args, kwargs, invocation_id, is_async=False, **options):
        if is_async:
            return AsyncResultWrapper(
                invocation_id,
                super().apply_async(
                    args=args, kwargs=kwargs, is_async=is_async, invocation_id=invocation_id, **options
                ),
                self,
                args,
                kwargs,
                options
            )
        else:
            return AsyncResultWrapper(
                invocation_id,
                super().apply(
                    args=args, kwargs=kwargs, is_async=is_async, invocation_id=invocation_id, **options
                ),
                self,
                args,
                kwargs,
                options
            )

    def _trigger(self, args, kwargs, invocation_id, task_id=None, eta=None, countdown=None, expires=None,
                 time_limit=None, stale_time_limit=None, is_async=True, **options):
        app = self._get_app()

        task_id = task_id or task_uuid()

        time_limit = self._get_time_limit(time_limit)
        trigger_time = now()
        eta = self._compute_eta(eta, countdown, trigger_time)
        countdown = None
        stale_time_limit = self._get_stale_time_limit(expires, time_limit, stale_time_limit, trigger_time)
        expires = self._compute_expires(expires, time_limit, stale_time_limit, trigger_time)

        options.update(dict(
            invocation_id=invocation_id,
            task_id=task_id,
            trigger_time=trigger_time,
            time_limit=time_limit,
            eta=eta,
            countdown=countdown,
            expires=expires,
            is_async=is_async,
            stale_time_limit=stale_time_limit
        ))

        unique_key = self._get_unique_key(args, kwargs)
        unique_task_id = self._get_unique_task_id(unique_key, task_id, stale_time_limit)

        if is_async and unique_task_id != task_id:
            options['task_id'] = unique_task_id
            self.on_invocation_unique(invocation_id, args, kwargs, unique_task_id, options)
            return AsyncResultWrapper(
                invocation_id,
                AsyncResult(unique_task_id, app=app),
                self,
                args,
                kwargs,
                options
            )
        else:
            self.on_invocation_trigger(invocation_id, args, kwargs, task_id, options)
            return self._apply_and_get_wrapped_result(args, kwargs, **options)

    def _first_apply(self, args=None, kwargs=None, invocation_id=None, is_async=True, is_on_commit=False, using=None,
                     **options):
        invocation_id = invocation_id or task_uuid()

        apply_time = now()
        app = self._get_app()
        queue = str(options.get('queue', getattr(self, 'queue', app.conf.task_default_queue)))

        options.update(dict(
            queue=queue,
            is_async=is_async,
            invocation_id=invocation_id,
            apply_time=apply_time,
            is_on_commit=is_on_commit,
            using=using,
        ))
        self.on_invocation_apply(invocation_id, args, kwargs, options)

        if is_on_commit:
            on_commit_result = OnCommitAsyncResult()
            self_inst = self

            def _apply_on_commit():
                result = self_inst._trigger(args=args, kwargs=kwargs, **options)
                on_commit_result.set_result(result)
            transaction.on_commit(_apply_on_commit, using=using)
            return on_commit_result
        else:
            return self._trigger(args=args, kwargs=kwargs, **options)

    def apply_async_on_commit(self, args=None, kwargs=None, using=None, **options):
        return self._first_apply(args=args, kwargs=kwargs, is_async=True, is_on_commit=True, using=using, **options)

    def apply(self, args=None, kwargs=None, **options):
        if 'retries' in options or 'is_async' in options:
            return super().apply(args=args, kwargs=kwargs, **options)
        else:
            return self._first_apply(args=args, kwargs=kwargs, is_async=False, **options)

    def apply_async(self, args=None, kwargs=None, **options):
        try:
            if self.request.id:
                return super().apply_async(args=args, kwargs=kwargs, **options)
            else:
                return self._first_apply(
                    args=args, kwargs=kwargs, is_async=True, **options
                )
        except (InterfaceError, OperationalError) as ex:
            logger.warn('Closing old database connections, following exception thrown: %s', str(ex))
            close_old_connections()
            raise ex

    def delay_on_commit(self, *args, **kwargs):
        options = kwargs.pop('options', {})
        self.apply_async_on_commit(args, kwargs, **options)

    def retry(self, args=None, kwargs=None, exc=None, throw=True,
              eta=None, countdown=None, max_retries=None, default_retry_delays=None, **options):

        if default_retry_delays or (eta is None and countdown is None and self.default_retry_delays):
            default_retry_delays = self.default_retry_delays if default_retry_delays is None else default_retry_delays
            max_retries = len(default_retry_delays)
            countdown = default_retry_delays[self.request.retries] if self.request.retries < max_retries else None

        if not eta and countdown is None:
            countdown = self.default_retry_delay

        if not eta:
            eta = now() + timedelta(seconds=countdown)

        self.on_task_retry(self.request.id, args, kwargs, exc, eta)

        return super().retry(
            args=args, kwargs=kwargs, exc=exc, throw=throw,
            eta=eta, max_retries=max_retries, **options
        )

    def apply_async_and_get_result(self, args=None, kwargs=None, timeout=None, propagate=True, **options):
        """
        Apply task in an asynchronous way, wait defined timeout and get AsyncResult or TimeoutError
        :param args: task args
        :param kwargs: task kwargs
        :param timeout: timout in seconds to wait for result
        :param propagate: propagate or not exceptions from celery task
        :param options: apply_async method options
        :return: AsyncResult or TimeoutError
        """
        result = self.apply_async(args=args, kwargs=kwargs, **options)
        if timeout is None or timeout > 0:
            return result.get(timeout=timeout, propagate=propagate)
        else:
            ex = TimeoutError('The operation timed out.')
            result.timeout(ex)
            raise ex

    def get_command_kwargs(self):
        return {}


def obj_to_string(obj):
    return base64.encodebytes(pickle.dumps(obj)).decode('utf8')


def string_to_obj(obj_string):
    return pickle.loads(base64.decodebytes(obj_string.encode('utf8')))


def get_django_command_task(command_name):
    if command_name not in current_app.tasks:
        raise ImproperlyConfigured(
            'Command was not found please check DJANGO_CELERY_EXTENSIONS_AUTO_GENERATE_TASKS_DJANGO_COMMANDS setting'
        )
    return current_app.tasks[command_name]


def auto_convert_commands_to_tasks():
    for name in get_commands():
        if name in settings.AUTO_GENERATE_TASKS_DJANGO_COMMANDS:
            def generate_command_task(command_name):
                shared_task_kwargs = dict(
                    base=import_string(settings.AUTO_GENERATE_TASKS_BASE),
                    bind=True,
                    name=command_name,
                    ignore_result=True,
                    **settings.AUTO_GENERATE_TASKS_DEFAULT_CELERY_KWARGS
                )
                shared_task_kwargs.update(settings.AUTO_GENERATE_TASKS_DJANGO_COMMANDS[command_name])

                @shared_task(
                    **shared_task_kwargs
                )
                def command_task(self, command_args=None, **kwargs):
                    command_args = [] if command_args is None else command_args
                    call_command(
                        command_name,
                        settings=os.environ.get('DJANGO_SETTINGS_MODULE'),
                        *command_args,
                        **self.get_command_kwargs()
                    )

            generate_command_task(name)
