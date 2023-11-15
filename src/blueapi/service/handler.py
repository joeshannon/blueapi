import logging
from multiprocessing import Pool
from typing import List, Mapping, Optional, Sequence, Type
from fastapi.encoders import jsonable_encoder

from pydantic import BaseModel, Field, parse_obj_as

from blueapi.config import ApplicationConfig
from blueapi.core import BlueskyContext
from blueapi.core.bluesky_types import Device
from blueapi.core.event import EventStream
from blueapi.data_management.visit_directory_provider import (
    LocalVisitServiceClient,
    VisitDirectoryProvider,
    VisitServiceClient,
    VisitServiceClientBase,
)
from blueapi.messaging import StompMessagingTemplate
from blueapi.messaging.base import MessagingTemplate
from blueapi.service.model import DeviceModel, PlanModel
from blueapi.utils.base_model import BlueapiBaseModel
from blueapi.worker.event import WorkerState
from blueapi.worker.reworker import RunEngineWorker
from blueapi.worker.task import RunPlan
from blueapi.worker.worker import TrackableTask, Worker


LOGGER = logging.getLogger(__name__)


class Simple(BaseModel):
    id: int
    name: str = "Jane Doe"


class Plan(BlueapiBaseModel):
    """
    A plan that can be run
    """

    name: str = Field(description="Referenceable name of the plan")
    description: Optional[str] = Field(
        description="Description/docstring of the plan", default=None
    )
    model: Type[BaseModel] = Field(
        description="Validation model of the parameters for the plan"
    )


class Handler:
    def __init__(self, config: Optional[ApplicationConfig] = None):
        self.config = config or ApplicationConfig()

    def start(self) -> None:
        self.proc = Pool(processes=1)
        self.proc.apply(setup_handler, [self.config])

    def stop(self) -> None:
        self.proc.apply(teardown_handler)
        self.proc.terminate()

    def get_plans(self) -> List[PlanModel]:
        ppp = self.proc.apply(get_plans)
        print()
        print(f"ppp: {ppp}")
        print(f"{ppp[0]}, type: {type(ppp[0])}")
        print()
        return ppp

    def get_plan(self, name: str) -> PlanModel:
        return self.proc.apply(get_plan, [name])

    def get_devices(self) -> List[DeviceModel]:
        return self.proc.apply(get_devices)

    def get_device(self, name: str) -> DeviceModel:
        return self.proc.apply(get_device, [name])

    def submit_task(self, task: RunPlan) -> str:
        return self.proc.apply(submit_task, [task])

    def clear_task(self, task_id: str) -> str:
        return self.proc.apply(clear_task, [task_id])

    def get_active_task(self) -> Optional[TrackableTask]:
        return self.proc.apply(get_active_task)

    def begin_task(self, task_id: str) -> None:
        self.proc.apply(begin_task, [task_id])

    def get_pending_task(self, task_id: str) -> Optional[TrackableTask]:
        return self.proc.apply(get_pending_task, [task_id])

    def get_state(self) -> WorkerState:
        return self.proc.apply(get_state)

    def pause(self, defer: bool) -> None:
        self.proc.apply(pause, [defer])

    def resume(self) -> None:
        self.proc.apply(resume)

    def cancel_active_task(self, failure: bool, reason: Optional[str]) -> None:
        self.proc.apply(cancel_active_task, [failure, reason])


class InternalHandler:
    """
    Manage worker and setup messaging.
    """

    context: BlueskyContext
    worker: Worker
    config: ApplicationConfig
    messaging_template: MessagingTemplate

    def __init__(
        self,
        config: Optional[ApplicationConfig] = None,
        context: Optional[BlueskyContext] = None,
        messaging_template: Optional[MessagingTemplate] = None,
        worker: Optional[Worker] = None,
    ) -> None:
        self.config = config or ApplicationConfig()
        self.context = context or BlueskyContext()

        self.context.with_config(self.config.env)

        self.worker = worker or RunEngineWorker(self.context)
        self.messaging_template = (
            messaging_template
            or StompMessagingTemplate.autoconfigured(self.config.stomp)
        )

    def start(self) -> None:
        self.worker.start()

        self._publish_event_streams(
            {
                self.worker.worker_events: self.messaging_template.destinations.topic(
                    "public.worker.event"
                ),
                self.worker.progress_events: self.messaging_template.destinations.topic(
                    "public.worker.event"
                ),
                self.worker.data_events: self.messaging_template.destinations.topic(
                    "public.worker.event"
                ),
            }
        )

        self.messaging_template.connect()

    def _publish_event_streams(
        self, streams_to_destinations: Mapping[EventStream, str]
    ) -> None:
        for stream, destination in streams_to_destinations.items():
            self._publish_event_stream(stream, destination)

    def _publish_event_stream(self, stream: EventStream, destination: str) -> None:
        stream.subscribe(
            lambda event, correlation_id: self.messaging_template.send(
                destination, event, None, correlation_id
            )
        )

    def stop(self) -> None:
        self.worker.stop()
        self.messaging_template.disconnect()


HANDLER: Optional[InternalHandler] = None


def setup_handler(
    config: Optional[ApplicationConfig] = None,
) -> None:
    global HANDLER

    provider = None
    plan_wrappers = []

    if config:
        visit_service_client: VisitServiceClientBase
        if config.env.data_writing.visit_service_url is not None:
            visit_service_client = VisitServiceClient(
                config.env.data_writing.visit_service_url
            )
        else:
            visit_service_client = LocalVisitServiceClient()

        provider = VisitDirectoryProvider(
            data_group_name=config.env.data_writing.group_name,
            data_directory=config.env.data_writing.visit_directory,
            client=visit_service_client,
        )

        # Make all dodal devices created by the context use provider if they can
        try:
            from dodal.parameters.gda_directory_provider import (
                set_directory_provider_singleton,
            )

            set_directory_provider_singleton(provider)
        except ImportError:
            logging.error(
                "Unable to set directory provider for ophyd-async devices, "
                "a newer version of dodal is required"
            )

        plan_wrappers.append(lambda plan: attach_metadata(plan, provider))

    handler = InternalHandler(
        config,
        context=BlueskyContext(
            plan_wrappers=plan_wrappers,
            sim=False,
        ),
    )
    handler.start()

    HANDLER = handler


def get_handler() -> InternalHandler:
    """Retrieve the handler which wraps the bluesky context, worker and message bus."""
    if HANDLER is None:
        raise ValueError()
    return HANDLER


def teardown_handler() -> None:
    """Stop all handler tasks. Does nothing if setup_handler has not been called."""
    global HANDLER
    if HANDLER is None:
        return
    handler = get_handler()
    handler.stop()
    HANDLER = None


# Static methods used in child process


def get_plans() -> List[PlanModel]:
    return [PlanModel.from_plan(p) for p in get_handler().context.plans.values()]


def get_plan(name: str) -> PlanModel:
    return PlanModel.from_plan(get_handler().context.plans[name])


def get_devices() -> List[DeviceModel]:
    return [DeviceModel.from_device(d) for d in get_handler().context.devices.values()]


def get_device(name: str) -> DeviceModel:
    return DeviceModel.from_device(get_handler().context.devices[name])


def submit_task(task: RunPlan) -> str:
    return get_handler().worker.submit_task(task)


def clear_task(task_id: str) -> str:
    return get_handler().worker.clear_task(task_id)


def get_active_task() -> Optional[TrackableTask]:
    return get_handler().worker.get_active_task()


def begin_task(task_id: str) -> None:
    get_handler().worker.begin_task(task_id)


def get_pending_task(task_id: str) -> Optional[TrackableTask]:
    return get_handler().worker.get_pending_task(task_id)


def get_state() -> WorkerState:
    return get_handler().worker.state


def pause(defer: bool) -> None:
    get_handler().worker.pause(defer)


def resume() -> None:
    get_handler().worker.resume()


def cancel_active_task(failure: bool, reason: Optional[str]) -> None:
    get_handler().worker.cancel_active_task(failure, reason)
