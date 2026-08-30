from __future__ import annotations

from src.data.dispatch import (
    DispatchValidationError,
    TwilioMode,
    VoiceProviderUnavailable,
)
from src.data.dispatch.broker import DispatchJob, SqsDispatchBroker
from src.data.dispatch.worker import DispatchWorker


class MalformedThenEmptyBroker:
    def __init__(self) -> None:
        self.receive_calls = 0
        self.acknowledged = []
        self.released = []

    def receive(self, *, wait_seconds: int = 20):
        del wait_seconds
        self.receive_calls += 1
        if self.receive_calls == 1:
            raise DispatchValidationError(
                "dispatch_job_invalid", "Dispatch queue message is invalid"
            )

    def acknowledge(self, job) -> None:
        self.acknowledged.append(job)

    def release(self, job, *, delay_seconds: int) -> None:
        self.released.append((job, delay_seconds))


class UnusedCoordinator:
    def dispatch_next(self, tenant_id: str, case_id: str):
        raise AssertionError(f"unexpected dispatch for {tenant_id}/{case_id}")


def test_malformed_queue_message_does_not_terminate_the_worker() -> None:
    broker = MalformedThenEmptyBroker()
    worker = DispatchWorker(
        broker=broker,
        coordinator=UnusedCoordinator(),
        mode=TwilioMode.MOCK,
    )

    assert worker.poll_once(wait_seconds=0) is True
    assert worker.poll_once(wait_seconds=0) is False
    assert broker.receive_calls == 2
    assert broker.acknowledged == []
    assert broker.released == []


def test_non_object_sqs_body_survives_and_is_left_for_redrive() -> None:
    class Client:
        def __init__(self) -> None:
            self.receive_calls = 0
            self.deleted = []
            self.released = []

        def receive_message(self, **kwargs):
            del kwargs
            self.receive_calls += 1
            if self.receive_calls == 1:
                return {
                    "Messages": [
                        {
                            "Body": "[]",
                            "ReceiptHandle": "malformed-receipt",
                            "Attributes": {"ApproximateReceiveCount": "1"},
                        }
                    ]
                }
            return {}

        def delete_message(self, **kwargs):
            self.deleted.append(kwargs)

        def change_message_visibility(self, **kwargs):
            self.released.append(kwargs)

    client = Client()
    broker = SqsDispatchBroker(
        "https://sqs.us-east-1.amazonaws.com/123456789012/dispatch",
        region_name="us-east-1",
        client=client,
    )
    worker = DispatchWorker(
        broker=broker,
        coordinator=UnusedCoordinator(),
        mode=TwilioMode.MOCK,
    )

    assert worker.poll_once(wait_seconds=0) is True
    assert worker.poll_once(wait_seconds=0) is False
    assert client.deleted == []
    assert client.released == []


def test_fifth_failed_delivery_terminalizes_case_and_is_not_released() -> None:
    tenant_id = "00000000-0000-4000-8000-000000000001"
    case_id = "00000000-0000-4000-8000-000000000002"

    class Broker:
        def __init__(self) -> None:
            self.job = DispatchJob(
                tenant_id=tenant_id,
                case_id=case_id,
                receipt_handle="receipt-5",
                receive_count=5,
            )
            self.acknowledged = []
            self.released = []

        def receive(self, *, wait_seconds: int = 20):
            del wait_seconds
            return self.job

        def acknowledge(self, job) -> None:
            self.acknowledged.append(job)

        def release(self, job, *, delay_seconds: int) -> None:
            self.released.append((job, delay_seconds))

    class Coordinator:
        def __init__(self) -> None:
            self.exhausted = []

        def dispatch_next(self, received_tenant_id: str, received_case_id: str):
            assert (received_tenant_id, received_case_id) == (tenant_id, case_id)
            raise VoiceProviderUnavailable()

        def mark_delivery_exhausted(
            self, received_tenant_id: str, received_case_id: str
        ) -> None:
            self.exhausted.append((received_tenant_id, received_case_id))

    broker = Broker()
    coordinator = Coordinator()
    worker = DispatchWorker(
        broker=broker,
        coordinator=coordinator,
        mode=TwilioMode.MOCK,
    )

    assert worker.poll_once(wait_seconds=0) is True
    assert coordinator.exhausted == [(tenant_id, case_id)]
    assert broker.acknowledged == []
    assert broker.released == []
