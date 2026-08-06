from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase, mock


MODULE_PATH = Path(__file__).with_name("main.py")
SPEC = importlib.util.spec_from_file_location("rvbbit_gmeet_connector", MODULE_PATH)
assert SPEC and SPEC.loader
gmeet = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gmeet
SPEC.loader.exec_module(gmeet)


class GoogleMeetConnectorTests(TestCase):
    def test_acl_policy_defaults_to_strict_calendar_invitees(self):
        with mock.patch.dict(gmeet.os.environ, {}, clear=True):
            settings = gmeet._settings({})
        self.assertEqual(settings["acl_mode"], "calendar_invitees_strict")

    def test_meeting_code_accepts_urls_and_ids(self):
        self.assertEqual(gmeet._meeting_code("https://meet.google.com/abc-defg-hij"), "abc-defg-hij")
        self.assertEqual(gmeet._meeting_code(None, "ABC-DEFG-HIJ"), "abc-defg-hij")
        self.assertIsNone(gmeet._meeting_code("not a meeting"))

    def test_participant_maps_directory_id_to_email(self):
        participant = {
            "name": "conferenceRecords/c1/participants/p1",
            "signedinUser": {"user": "users/123", "displayName": "Ada Lovelace"},
        }
        identity = gmeet._participant_identity(participant, {"123": "ada@example.com"})
        self.assertEqual(identity["email"], "ada@example.com")
        self.assertEqual(identity["displayName"], "Ada Lovelace")
        self.assertEqual(identity["kind"], "signed_in")

    def test_calendar_match_uses_the_nearest_recurring_instance(self):
        events = [
            {"id": "older", "start": {"dateTime": "2026-08-01T14:00:00Z"}},
            {"id": "matching", "start": {"dateTime": "2026-08-05T14:00:00Z"}},
            {"id": "future", "start": {"dateTime": "2026-08-12T14:00:00Z"}},
        ]
        event = gmeet._calendar_event_for_record(events, "2026-08-05T14:03:00Z")
        self.assertEqual(event["id"], "matching")

    def test_health_requires_credentials_and_a_delegated_subject(self):
        response = gmeet.Response()
        with mock.patch.object(gmeet, "SA_KEY", ""), mock.patch.dict(gmeet.os.environ, {}, clear=True):
            result = gmeet.health(response)
        self.assertFalse(result["ok"])
        self.assertEqual(response.status_code, 503)

        response = gmeet.Response()
        with mock.patch.object(gmeet, "SA_KEY", "inline-json"), \
                mock.patch.object(
                    gmeet,
                    "_credential_info",
                    return_value={"type": "service_account", "client_email": "meet@example.com"},
                ), \
                mock.patch.dict(
                    gmeet.os.environ,
                    {"GMEET_ADMIN_SUBJECT": "admin@example.com"},
                    clear=True,
                ):
            result = gmeet.health(response)
        self.assertTrue(result["ok"])
        self.assertEqual(response.status_code, 200)

    def _patch_sync(self, *, acl_mode="calendar_invitees_strict", calendar_event=True):
        settings = {
            "subjects": ["owner@example.com", "attendee@example.com"],
            "admin_subject": "owner@example.com",
            "domain": "example.com",
            "lookback_days": 29,
            "max_subjects": 500,
            "discover_users": True,
            "calendar_lookup": True,
            "auto_transcribe": False,
            "auto_transcribe_days": 7,
            "drive_acl": True,
            "acl_mode": acl_mode,
        }
        record = {
            "name": "conferenceRecords/c1",
            "space": "spaces/s1",
            "startTime": "2026-08-05T14:00:00Z",
            "endTime": "2026-08-05T14:30:00Z",
        }
        event = {
            "id": "cal1",
            "summary": "Weekly Revenue Review",
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
            "htmlLink": "https://calendar.google.com/event?eid=cal1",
            "organizer": {"email": "owner@example.com"},
            "attendees": [
                {"email": "owner@example.com", "organizer": True, "responseStatus": "accepted"},
                {"email": "invitee@example.com", "responseStatus": "accepted"},
            ],
        }
        transcript = {
            "name": "conferenceRecords/c1/transcripts/t1",
            "state": "FILE_GENERATED",
            "endTime": "2026-08-05T14:31:00Z",
            "docsDestination": {
                "document": "doc1",
                "exportUri": "https://docs.google.com/document/d/doc1/edit",
            },
        }
        participants = [{
            "name": "conferenceRecords/c1/participants/p1",
            "signedinUser": {"user": "users/u1", "displayName": "Owner Person"},
        }]
        entries = [{
            "participant": "conferenceRecords/c1/participants/p1",
            "text": "The renewal forecast is ready.",
            "startTime": "2026-08-05T14:01:02Z",
            "endTime": "2026-08-05T14:01:08Z",
        }]

        patches = [
            mock.patch.object(gmeet, "_settings", return_value=settings),
            mock.patch.object(
                gmeet,
                "_resolve_subjects",
                return_value=(settings["subjects"], {"u1": "owner@example.com"}),
            ),
            mock.patch.object(gmeet, "_now", return_value=datetime(2026, 8, 6, tzinfo=timezone.utc)),
            mock.patch.object(
                gmeet,
                "_calendar_index",
                return_value=({"abc-defg-hij": [event]} if calendar_event else {}, 2),
            ),
            mock.patch.object(gmeet, "_list_conferences", return_value=[record]),
            mock.patch.object(
                gmeet,
                "_space",
                return_value={
                    "name": "spaces/s1",
                    "meetingCode": "abc-defg-hij",
                    "meetingUri": "https://meet.google.com/abc-defg-hij",
                },
            ),
            mock.patch.object(gmeet, "_list_transcripts", return_value=[transcript]),
            mock.patch.object(gmeet, "_list_participants", return_value=participants),
            mock.patch.object(gmeet, "_list_entries", return_value=entries),
            mock.patch.object(
                gmeet,
                "_drive_details",
                return_value=(
                    {
                        "id": "doc1",
                        "name": "Meet transcript",
                        "modifiedTime": "2026-08-05T14:32:00Z",
                        "version": "7",
                        "owners": [{"emailAddress": "owner@example.com"}],
                    },
                    ["owner@example.com", "shared-but-not-invited@example.com"],
                    [{"folder_id": "doc1", "grant_kind": "group", "grant_value": "sales@example.com"}],
                ),
            ),
        ]
        return patches

    def test_sync_deduplicates_meeting_and_returns_governed_manifest(self):
        patches = self._patch_sync()
        with patches[0], patches[1], patches[2], patches[3], patches[4] as conferences, \
                patches[5], patches[6], patches[7], patches[8], patches[9]:
            result = gmeet._sync(gmeet.SyncRequest(source_id=9))

        self.assertEqual(conferences.call_count, 2)
        self.assertEqual(len(result["files"]), 1)
        row = result["files"][0]
        self.assertEqual(row["title"], "Weekly Revenue Review")
        self.assertEqual(row["author"], "owner@example.com")
        self.assertEqual(row["occurred_at"], "2026-08-05T14:00:00Z")
        self.assertEqual(row["permissions"], ["invitee@example.com", "owner@example.com"])
        self.assertNotIn("shared-but-not-invited@example.com", row["permissions"])
        self.assertIn("Owner Person <owner@example.com>", row["body"])
        self.assertIn("The renewal forecast is ready.", row["body"])
        self.assertEqual(row["props"]["docType"], "meeting")
        self.assertEqual(result["stats"]["conference_records_seen"], 2)
        self.assertEqual(result["stats"]["conference_records_unique"], 1)
        self.assertEqual(result["stats"]["transcript_files"], 1)
        self.assertEqual(result["pending_grants"], [])
        self.assertEqual(result["stats"]["drive_grants_ignored"], 1)

    def test_unchanged_transcript_returns_metadata_without_body(self):
        patches = self._patch_sync()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], patches[9]:
            first = gmeet._sync(gmeet.SyncRequest(source_id=9))
            uri = first["files"][0]["uri"]
            content_hash = first["files"][0]["content_hash"]
            second = gmeet._sync(gmeet.SyncRequest(source_id=9, known={uri: content_hash}))
        self.assertNotIn("body", second["files"][0])

    def test_drive_and_calendar_is_explicit_legacy_opt_in(self):
        patches = self._patch_sync(acl_mode="drive_and_calendar")
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], patches[9]:
            result = gmeet._sync(gmeet.SyncRequest(source_id=9))
        self.assertIn("invitee@example.com", result["files"][0]["permissions"])
        self.assertIn("shared-but-not-invited@example.com", result["files"][0]["permissions"])
        self.assertEqual(result["pending_grants"][0]["grant_kind"], "group")

    def test_drive_acl_mode_is_explicit_and_does_not_add_calendar_invitees(self):
        patches = self._patch_sync(acl_mode="drive")
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], patches[9]:
            result = gmeet._sync(gmeet.SyncRequest(source_id=9))
        self.assertEqual(
            result["files"][0]["permissions"],
            ["owner@example.com", "shared-but-not-invited@example.com"],
        )
        self.assertNotIn("invitee@example.com", result["files"][0]["permissions"])

    def test_strict_calendar_acl_fails_closed_to_organizer_when_event_is_unresolved(self):
        patches = self._patch_sync(calendar_event=False)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], patches[9]:
            result = gmeet._sync(gmeet.SyncRequest(source_id=9))
        self.assertEqual(result["files"][0]["permissions"], ["owner@example.com"])
        self.assertEqual(result["stats"]["acl_calendar_unresolved"], 1)
        self.assertTrue(any("restricted to the known organizer" in warning
                            for warning in result["stats"]["warning_samples"]))

    def test_auto_transcription_is_explicit_and_uses_the_organizer(self):
        event = {
            "id": "future-1",
            "start": {"dateTime": "2026-08-07T14:00:00Z"},
            "organizer": {"email": "owner@example.com"},
        }
        warnings = []
        with mock.patch.object(gmeet, "_ensure_auto_transcription", return_value=True) as enable:
            stats = gmeet._configure_upcoming_transcriptions(
                {"abc-defg-hij": [event]},
                ["owner@example.com", "attendee@example.com"],
                datetime(2026, 8, 6, tzinfo=timezone.utc),
                7,
                warnings,
            )
        enable.assert_called_once_with("owner@example.com", "abc-defg-hij")
        self.assertEqual(stats["enabled"], 1)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(warnings, [])

    def test_auto_transcription_patch_preserves_other_space_settings(self):
        spaces = mock.Mock()
        spaces.get.return_value = mock.Mock()
        spaces.patch.return_value = mock.Mock()
        service = mock.Mock()
        service.spaces.return_value = spaces
        with mock.patch.object(gmeet, "_service", return_value=service), \
                mock.patch.object(
                    gmeet,
                    "_execute",
                    side_effect=[
                        {
                            "name": "spaces/canonical-id",
                            "config": {
                                "moderation": "ON",
                                "artifactConfig": {
                                    "transcriptionConfig": {
                                        "autoTranscriptionGeneration": "OFF",
                                    },
                                },
                            },
                        },
                        {"name": "spaces/canonical-id"},
                    ],
                ):
            changed = gmeet._ensure_auto_transcription("owner@example.com", "abc-defg-hij")
        self.assertTrue(changed)
        kwargs = spaces.patch.call_args.kwargs
        self.assertEqual(kwargs["name"], "spaces/canonical-id")
        self.assertEqual(
            kwargs["updateMask"],
            "config.artifactConfig.transcriptionConfig.autoTranscriptionGeneration",
        )
        self.assertNotIn("moderation", kwargs["body"]["config"])


if __name__ == "__main__":
    import unittest

    unittest.main()
