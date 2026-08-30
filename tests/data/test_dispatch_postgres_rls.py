from __future__ import annotations

import os
from pathlib import Path

import pytest

DSN = os.getenv("TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="Set TEST_POSTGRES_DSN to run direct PostgreSQL dispatch/RLS tests",
)


def test_dispatch_tables_enforce_confirmation_attempt_cap_and_tenant_rls() -> None:
    psycopg = pytest.importorskip("psycopg")
    from src.data.postgres import TenantPostgres

    root = Path(__file__).resolve().parents[2]
    with psycopg.connect(DSN, autocommit=True) as connection:
        for migration in sorted((root / "migrations" / "postgres").glob("*.sql")):
            connection.execute(migration.read_text(encoding="utf-8"))

    tenant_a = "71111111-1111-4111-8111-111111111111"
    tenant_b = "72222222-2222-4222-8222-222222222222"
    source_id = "7aaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    asset_id = "7bbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    detection_id = "7ccccccc-cccc-4ccc-8ccc-cccccccccccc"
    review_id = "7ddddddd-dddd-4ddd-8ddd-dddddddddddd"
    incident_id = "7eeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    primary_id = "71000000-0000-4000-8000-000000000001"
    supervisor_id = "71000000-0000-4000-8000-000000000002"
    dispatch_case_id = "72000000-0000-4000-8000-000000000001"
    promoted_event_id = "dispatch-test-promoted-event"
    database = TenantPostgres(DSN)
    try:
        with database.transaction(tenant_a) as cursor:
            cursor.execute(
                """INSERT INTO camera_sources (tenant_id,source_id,definition)
                   VALUES (%s,%s,'{}'::jsonb) ON CONFLICT DO NOTHING""",
                (tenant_a, source_id),
            )
            cursor.execute(
                """INSERT INTO video_assets_restricted
                     (tenant_id,asset_id,source_id,metadata)
                   VALUES (%s,%s,%s,'{}'::jsonb) ON CONFLICT DO NOTHING""",
                (tenant_a, asset_id, source_id),
            )
            cursor.execute(
                """INSERT INTO candidate_detections_restricted
                     (tenant_id,detection_id,source_id,asset_id,semantic_key,candidate)
                   VALUES (%s,%s,%s,%s,'dispatch-contract-test','{}'::jsonb)
                   ON CONFLICT DO NOTHING""",
                (tenant_a, detection_id, source_id, asset_id),
            )
            cursor.execute(
                """INSERT INTO candidate_reviews_restricted
                     (tenant_id,review_id,detection_id,decision)
                   VALUES (%s,%s,%s,
                     jsonb_build_object(
                       'decision','confirmed',
                       'promoted_external_event_id',%s::text
                     ))
                   ON CONFLICT DO NOTHING""",
                (tenant_a, review_id, detection_id, promoted_event_id),
            )
            cursor.execute(
                """INSERT INTO accepted_incident_events_restricted (
                     tenant_id,source_id,external_event_id,event,event_hash,
                     occurred_at,received_at,category,latitude,longitude
                   ) VALUES (
                     %s,%s,%s,'{}'::jsonb,%s,now(),now(),
                     'traffic_safety',0.0,0.0
                   ) ON CONFLICT DO NOTHING""",
                (tenant_a, source_id, promoted_event_id, "b" * 64),
            )
            for contact_id, role, suffix in (
                (primary_id, "primary", "primary"),
                (supervisor_id, "supervisor", "supervisor"),
            ):
                cursor.execute(
                    """INSERT INTO response_contacts (
                         tenant_id,contact_id,zone_id,broad_location_label,
                         coverage_h3_cells,role,contact_label,
                         destination_secret_ref,masked_destination,timezone,
                         calling_window_start,calling_window_end,opted_in_at,verified_at
                       ) VALUES (
                         %s,%s,'dispatch-test-zone','Dispatch Test Zone',
                         ARRAY['8860145b49fffff'],%s,%s,%s,'+1 ******0100','UTC',
                         '00:00','23:59',now(),now()
                       ) ON CONFLICT DO NOTHING""",
                    (
                        tenant_a,
                        contact_id,
                        role,
                        f"Dispatch test {role}",
                        f"secret://dispatch-test/{suffix}",
                    ),
                )
            cursor.execute(
                """INSERT INTO dispatch_cases (
                     tenant_id,dispatch_case_id,incident_id,review_id,
                     incident_source_id,incident_external_event_id,case_reference,
                     confirmed_category,occurred_at,broad_location_label,zone_id,
                     primary_contact_id,supervisor_contact_id,call_authorized,
                     authorized_by,authorized_at,authorization_fingerprint,
                     idempotency_key_hash,
                     policy_version,message_template_version,retry_delay_seconds
                   ) VALUES (
                     %s,%s,%s,%s,%s,%s,'CH-DB-7001','traffic_safety',
                     now(),'Dispatch Test Zone','dispatch-test-zone',%s,%s,true,
                     'dispatch-test-reviewer',now(),%s,%s,
                     'voice-escalation-v1','dispatch-demo-en-v1',30
                   ) ON CONFLICT DO NOTHING""",
                (
                    tenant_a,
                    dispatch_case_id,
                    incident_id,
                    review_id,
                    source_id,
                    promoted_event_id,
                    primary_id,
                    supervisor_id,
                    "f" * 64,
                    "a" * 64,
                ),
            )

        with database.transaction(tenant_a) as cursor:
            for attempt_number, contact_id, role in (
                (1, primary_id, "primary"),
                (2, primary_id, "primary"),
                (3, supervisor_id, "supervisor"),
            ):
                cursor.execute(
                    """INSERT INTO dispatch_call_attempts (
                         tenant_id,attempt_id,dispatch_case_id,attempt_number,
                         recipient_role,contact_id,scheduled_at
                       ) VALUES (%s,%s,%s,%s,%s,%s,now())
                       ON CONFLICT DO NOTHING""",
                    (
                        tenant_a,
                        f"73000000-0000-4000-8000-00000000000{attempt_number}",
                        dispatch_case_id,
                        attempt_number,
                        role,
                        contact_id,
                    ),
                )

        with (
            pytest.raises(psycopg.errors.CheckViolation),
            database.transaction(tenant_a) as cursor,
        ):
            cursor.execute(
                """INSERT INTO dispatch_call_attempts (
                     tenant_id,attempt_id,dispatch_case_id,attempt_number,
                     recipient_role,contact_id,scheduled_at
                   ) VALUES (%s,'73000000-0000-4000-8000-000000000004',%s,4,
                     'supervisor',%s,now())""",
                (tenant_a, dispatch_case_id, supervisor_id),
            )

        with database.transaction(tenant_b) as cursor:
            cursor.execute(
                "SELECT dispatch_case_id FROM dispatch_cases WHERE dispatch_case_id=%s",
                (dispatch_case_id,),
            )
            assert cursor.fetchone() is None

        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            database.transaction(tenant_b) as cursor,
        ):
            cursor.execute(
                """INSERT INTO response_contacts (
                     tenant_id,contact_id,zone_id,broad_location_label,
                     coverage_h3_cells,role,contact_label,
                     destination_secret_ref,masked_destination,timezone,
                     calling_window_start,calling_window_end,opted_in_at,verified_at
                   ) VALUES (%s,'74000000-0000-4000-8000-000000000001',
                     'cross-tenant-zone','Cross tenant zone',ARRAY['8860145b49fffff'],
                     'primary','Cross tenant contact','secret://cross-tenant/contact',
                     '+1 ******0101','UTC','00:00','23:59',now(),now())""",
                (tenant_a,),
            )
    finally:
        database.close()
