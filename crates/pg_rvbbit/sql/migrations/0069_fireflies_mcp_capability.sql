-- 0069_fireflies_mcp_capability — add Fireflies.ai (meeting transcripts/summaries) to the MCP catalog.
--
-- Fireflies is a REMOTE MCP (https://api.fireflies.ai/mcp) bridged to stdio via `npx mcp-remote`. The
-- API key is carried in the request HEADER, and `mcp-remote` substitutes ${VAR} in --header values from
-- the process ENV — so the secret goes in connection.env (the gateway's resolve_env injects it); if it's
-- absent mcp-remote falls back to interactive OAuth and HANGS headless. No gateway code change needed.
--
-- Registered via upsert_capability_catalog_entry with catalog_source='community' (NOT 'curated'): the
-- curated seed (capability_catalog_seed.json) is pruned per-source, so this survives re-seeding without
-- editing that file. The tools/operators below were scanned from a live install (refresh_mcp_server) and
-- baked in, so a FRESH deploy ships the full surface (like the curated github/linear entries). Install
-- from the lens MCP catalog → prompts FIREFLIES_API_KEY → ready. Tools: https://docs.fireflies.ai/mcp-tools/overview

SELECT rvbbit.upsert_capability_catalog_entry(
    catalog_entry => $ce$
{
    "id": "mcp/fireflies",
    "kind": "mcp",
    "name": "fireflies",
    "tags": [
        "mcp",
        "meetings",
        "transcripts",
        "notes",
        "saas"
    ],
    "title": "Fireflies (MCP)",
    "operators": [
        "fireflies_create_soundbite",
        "fireflies_fetch",
        "fireflies_get_active_meetings",
        "fireflies_get_analytics",
        "fireflies_get_channel",
        "fireflies_get_rule_executions",
        "fireflies_get_soundbites",
        "fireflies_get_summary",
        "fireflies_get_transcript",
        "fireflies_get_transcripts",
        "fireflies_get_user",
        "fireflies_get_user_contacts",
        "fireflies_get_usergroups",
        "fireflies_list_channels",
        "fireflies_move_meeting",
        "fireflies_revoke_meeting_access",
        "fireflies_search",
        "fireflies_share_meeting",
        "fireflies_update_meeting_privacy",
        "fireflies_update_meeting_title"
    ],
    "description": "Fireflies.ai meeting transcripts & summaries via the remote MCP (npx mcp-remote → https://api.fireflies.ai/mcp). Bearer FIREFLIES_API_KEY is injected into the request header by the gateway. Tool list populates on install/refresh.",
    "manifest_path": "mcp/fireflies",
    "catalog_visibility": "public"
}
$ce$::jsonb,
    capability_manifest => $cm$
{
    "kind": "mcp",
    "name": "fireflies",
    "tools": [
        {
            "name": "fireflies_create_soundbite",
            "cacheable": false,
            "description": "Creates a soundbite (clip) from a meeting transcript. A soundbite is a short audio/video segment extracted from a meeting. The authenticated user must have write access to the meeting. Requires the transcript ID and start/end times (in seconds).",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "required": [
                    "transcriptId",
                    "startTime",
                    "endTime"
                ],
                "properties": {
                    "name": {
                        "type": "string",
                        "maxLength": 256,
                        "description": "Optional name/title for the soundbite (max 256 characters)"
                    },
                    "endTime": {
                        "type": "number",
                        "description": "End time of the soundbite in seconds (must be > 0 and > startTime)",
                        "exclusiveMinimum": 0
                    },
                    "summary": {
                        "type": "string",
                        "maxLength": 500,
                        "description": "Optional summary text for the soundbite (max 500 characters)"
                    },
                    "mediaType": {
                        "type": "string",
                        "maxLength": 10,
                        "description": "Optional media type (e.g. \"audio\", \"video\")"
                    },
                    "privacies": {
                        "type": "array",
                        "items": {
                            "enum": [
                                "public",
                                "team",
                                "participants"
                            ],
                            "type": "string"
                        },
                        "description": "Optional privacy settings for the soundbite. Values: \"public\", \"team\", \"participants\""
                    },
                    "startTime": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Start time of the soundbite in seconds (must be >= 0)"
                    },
                    "transcriptId": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The transcript ID / meeting ID to create the soundbite from"
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_fetch",
            "cacheable": false,
            "description": "Retrieve complete meeting transcript with full conversation, metadata, and insights for a specific meeting ID. Use this after search to get detailed content.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "required": [
                    "id"
                ],
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Meeting transcript ID obtained from search results"
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_get_active_meetings",
            "cacheable": false,
            "description": "Retrieves a list of currently active (in-progress) meetings from Fireflies.ai. Returns meeting details including ID, title, organizer, meeting link, start/end time, privacy, and state. Admins can query active meetings for any user; regular users can only query their own.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "properties": {
                    "email": {
                        "type": "string",
                        "format": "email",
                        "description": "Optional email address to filter active meetings by a specific user. Admins can query any team member; regular users can only use their own email or omit this field."
                    },
                    "states": {
                        "type": "array",
                        "items": {
                            "enum": [
                                "active",
                                "paused"
                            ],
                            "type": "string"
                        },
                        "description": "Optional array of meeting states to filter by. Possible values: \"active\" (currently in progress), \"paused\" (meeting has been paused). If omitted, returns meetings in both states."
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_get_analytics",
            "cacheable": false,
            "description": "Retrieves team and per-user meeting and conversation analytics from Fireflies.ai. Returns meeting counts, durations, conversation metrics (filler words, questions, monologues, sentiment, talk-listen ratio, words per minute), and comparison data against previous periods. Accepts optional start_time and end_time filters in ISO 8601 format. Team-level analytics require admin privileges.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "properties": {
                    "endTime": {
                        "type": "string",
                        "format": "date-time",
                        "description": "Optional end date/time filter in ISO 8601 format (e.g. \"2024-12-31T23:59:59.999Z\"). Limits analytics to meetings before this time."
                    },
                    "startTime": {
                        "type": "string",
                        "format": "date-time",
                        "description": "Optional start date/time filter in ISO 8601 format (e.g. \"2024-01-01T00:00:00.000Z\"). Limits analytics to meetings after this time."
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_get_channel",
            "cacheable": false,
            "description": "Retrieves details of a specific channel/folder by its ID. Returns channel title, privacy setting, and member list.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "required": [
                    "channelId"
                ],
                "properties": {
                    "channelId": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The ID of the channel to retrieve"
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_get_rule_executions",
            "cacheable": false,
            "description": "Retrieves rule execution logs grouped by meeting. Shows which automation rules were triggered on meetings, including the actions taken (sharing, moving to channels, changing privacy). Requires Enterprise tier access. Results are paginated.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "maximum": 50,
                        "minimum": 1,
                        "description": "Maximum number of meeting groups to return (default: 10, max: 50)"
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor from a previous response to fetch the next page"
                    },
                    "dateTo": {
                        "type": "string",
                        "description": "Filter executions up to this date (ISO 8601 format, e.g. 2024-12-31T23:59:59Z)"
                    },
                    "ruleId": {
                        "type": "string",
                        "description": "Filter by a specific rule ID"
                    },
                    "dateFrom": {
                        "type": "string",
                        "description": "Filter executions from this date (ISO 8601 format, e.g. 2024-01-01T00:00:00Z)"
                    },
                    "meetingId": {
                        "type": "string",
                        "description": "Filter by a specific meeting ID"
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_get_soundbites",
            "cacheable": false,
            "description": "Fetches soundbites (short shareable audio/transcript clips from meetings). Can filter by transcript, ownership, or team. At least one of mine, transcript_id, or my_team must be provided.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "properties": {
                    "mine": {
                        "type": "boolean",
                        "description": "Optional filter to only include soundbites owned by the authenticated user"
                    },
                    "skip": {
                        "type": "number",
                        "description": "Optional number of soundbites to skip for pagination"
                    },
                    "limit": {
                        "type": "number",
                        "maximum": 50,
                        "description": "Optional limit for the number of soundbites to return (max 50)"
                    },
                    "format": {
                        "enum": [
                            "toon",
                            "json",
                            "text"
                        ],
                        "type": "string",
                        "default": "toon",
                        "description": "Optional response format: \"toon\" (default, token-efficient), \"json\" (standard JSON), or \"text\" (human-readable)"
                    },
                    "my_team": {
                        "type": "boolean",
                        "description": "Optional filter to include soundbites from the authenticated user's team"
                    },
                    "transcript_id": {
                        "type": "string",
                        "maxLength": 100,
                        "description": "Optional transcript/meeting ID to get soundbites for a specific meeting"
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_get_summary",
            "cacheable": false,
            "description": "Fetches meeting summary by ID, with optional field filtering. Returns summary data (keywords, action items, overview, etc.) and basic metadata, but excludes transcript content. If you need transcript content, use fireflies_get_transcript instead.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "required": [
                    "transcriptId"
                ],
                "properties": {
                    "transcriptId": {
                        "type": "string",
                        "description": "The meeting ID to fetch summary for"
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_get_transcript",
            "cacheable": false,
            "description": "Fetches detailed meeting transcript by ID, with optional field filtering. Returns transcript content (sentences, speakers) and metadata, but excludes summary data. If you need summary data, use fireflies_get_summary instead. If the meeting is currently live (is_live: true), the transcript returned is a point-in-time snapshot of the live transcript with sentences captured so far.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "required": [
                    "transcriptId"
                ],
                "properties": {
                    "transcriptId": {
                        "type": "string",
                        "description": "The meeting ID to fetch"
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_get_transcripts",
            "cacheable": false,
            "description": "Queries multiple meeting transcripts using filter properties (date, keyword, email, etc.). Returns basic metadata and transcript summary. Does NOT accept transcriptId as input - use fireflies_get_transcript() multiple times to get detailed transcript content.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "properties": {
                    "date": {
                        "type": "number",
                        "description": "Optional date filter (deprecated)"
                    },
                    "mine": {
                        "type": "boolean",
                        "description": "Optional filter to only include meetings owned by the authenticated user"
                    },
                    "skip": {
                        "type": "number",
                        "description": "Optional number of meetings to skip for pagination"
                    },
                    "limit": {
                        "type": "number",
                        "maximum": 50,
                        "description": "Optional limit for the number of meetings to return (max 50)"
                    },
                    "scope": {
                        "enum": [
                            "title",
                            "sentences",
                            "all"
                        ],
                        "type": "string",
                        "description": "Optional scope for keyword search: \"title\" (meeting titles only), \"sentences\" (transcript content), or \"all\" (both)"
                    },
                    "format": {
                        "enum": [
                            "toon",
                            "json",
                            "text"
                        ],
                        "type": "string",
                        "default": "toon",
                        "description": "Optional response format: \"toon\" (default, token-efficient), \"json\" (standard JSON), or \"text\" (human-readable)"
                    },
                    "toDate": {
                        "type": "string",
                        "description": "Optional ISO 8601 date string (e.g., \"2023-12-31\") to filter meetings until this date"
                    },
                    "keyword": {
                        "type": "string",
                        "maxLength": 255,
                        "description": "Optional keyword to search for in meeting content"
                    },
                    "fromDate": {
                        "type": "string",
                        "description": "Optional ISO 8601 date string (e.g., \"2023-01-01\") to filter meetings from this date"
                    },
                    "organizers": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "format": "email"
                        },
                        "description": "Optional array of organizer email addresses to filter meetings"
                    },
                    "participants": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "format": "email"
                        },
                        "description": "Optional array of participant email addresses to filter meetings"
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_get_user",
            "cacheable": false,
            "description": "Fetches user account details. Returns profile info, transcript counts, meeting activity, and admin status. If no user ID provided, returns current authenticated user data.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "properties": {
                    "userId": {
                        "type": "string",
                        "description": "Optional user ID. If not provided, returns the current authenticated user's details."
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_get_user_contacts",
            "cacheable": false,
            "description": "Fetches contact list for the authenticated user. Returns contacts with their names, emails, profile pictures, and last meeting dates sorted by most recent interaction.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "properties": {
                    "format": {
                        "enum": [
                            "toon",
                            "json",
                            "text"
                        ],
                        "type": "string",
                        "default": "toon",
                        "description": "Optional response format: \"toon\" (default, token-efficient), \"json\" (standard JSON), or \"text\" (human-readable)"
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_get_usergroups",
            "cacheable": false,
            "description": "Fetches user groups for the authenticated user or their team. Returns group details including name, handle, and members. Use mine=true to get only groups the user belongs to, or mine=false (default) to get all groups in the team.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "properties": {
                    "mine": {
                        "type": "boolean",
                        "description": "Optional filter. If true, returns only groups the authenticated user belongs to. If false (default), returns all groups in the user's team."
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_list_channels",
            "cacheable": false,
            "description": "Lists all channels/folders available to the authenticated user. Returns channel details including ID, title, privacy setting, and members.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "properties": {
                }
            }
        },
        {
            "name": "fireflies_move_meeting",
            "cacheable": false,
            "description": "Moves one or more meeting transcripts to a specified channel/folder. The authenticated user must be the owner of the meetings or a team admin. Up to 5 meeting IDs can be provided.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "required": [
                    "meetingIds",
                    "channelId"
                ],
                "properties": {
                    "channelId": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The target channel/folder ID to move the meetings to"
                    },
                    "meetingIds": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "minLength": 1
                        },
                        "maxItems": 5,
                        "minItems": 1,
                        "description": "Array of meeting IDs / transcript IDs to move (max 5)"
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_revoke_meeting_access",
            "cacheable": false,
            "description": "Revokes a previously shared meeting access for a specific email address. The authenticated user must be the owner of the meeting or a team admin.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "required": [
                    "meetingId",
                    "email"
                ],
                "properties": {
                    "email": {
                        "type": "string",
                        "format": "email",
                        "description": "The email address to revoke access for"
                    },
                    "meetingId": {
                        "type": "string",
                        "description": "The meetingId / transcriptId to revoke access from"
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_search",
            "cacheable": false,
            "description": "Advanced search for meeting transcripts using a mini grammar. Supports complex queries with multiple filters.\n\nGRAMMAR SYNTAX:\n- keyword:\"search term\" - Search for keywords in the content. If no scope is specified, the default scope is 'all'.\n- scope:title|sentences|all - Define the search scope. Options are 'title', 'sentences', or 'all'. The default is 'all'.\n- from:YYYY-MM-DD - Filter meetings from this date (ISO format)\n- to:YYYY-MM-DD - Filter meetings until this date (ISO format)\n- limit:N - Limit results (max 50)\n- skip:N - Skip N results for pagination\n- organizers:email1@x.com,email2@x.com - Filter by organizer emails (comma-separated)\n- participants:email1@x.com,email2@x.com - Filter by participant emails (comma-separated)\n- mine:true|false - Filter to only include user's own meetings\n\nEXAMPLES:\n- \"engineering standup\" (simple keyword search)\n- \"keyword:\\\"performance\\\" scope:sentences limit:20\"",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "required": [
                    "query"
                ],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query using the mini grammar syntax. Can be simple keywords or complex filters using the grammar."
                    },
                    "format": {
                        "enum": [
                            "toon",
                            "json",
                            "text"
                        ],
                        "type": "string",
                        "default": "toon",
                        "description": "Optional response format: \"toon\" (default, token-efficient), \"json\" (standard JSON), or \"text\" (human-readable)"
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_share_meeting",
            "cacheable": false,
            "description": "Shares a meeting transcript with specified email addresses. The authenticated user must be the owner of the meeting or a team admin. Up to 100 emails can be provided. Optionally set an expiry period for the shared access.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "required": [
                    "meetingId",
                    "emails"
                ],
                "properties": {
                    "emails": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "format": "email"
                        },
                        "maxItems": 100,
                        "minItems": 1,
                        "description": "Array of email addresses to share the meeting with (max 100)"
                    },
                    "meetingId": {
                        "type": "string",
                        "description": "The meetingId / transcriptId to share"
                    },
                    "expiryDays": {
                        "enum": [
                            7,
                            14,
                            30
                        ],
                        "type": "number",
                        "description": "Optional expiry period in days for the shared access. Must be one of: 7, 14, 30"
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_update_meeting_privacy",
            "cacheable": false,
            "description": "Updates the privacy setting of a meeting transcript. The authenticated user must be the owner of the meeting or a team admin. Privacy options: \"owner\" (only the owner can view), \"participants\" (only meeting participants can view), \"participatingteammates\" (only participants who are teammates can view), \"teammates\" (all teammates can view), \"teammatesandparticipants\" (teammates and participants can view), \"link\" (teammates and anyone with the link can view).",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "required": [
                    "meetingId",
                    "privacy"
                ],
                "properties": {
                    "privacy": {
                        "enum": [
                            "link",
                            "owner",
                            "participants",
                            "participatingteammates",
                            "teammatesandparticipants",
                            "teammates"
                        ],
                        "type": "string",
                        "description": "The privacy level to set. Options: \"owner\" (only owner), \"participants\" (meeting participants), \"participatingteammates\" (participants in the team), \"teammates\" (all teammates), \"teammatesandparticipants\" (teammates and participants), \"link\" (teammates and anyone with the link)"
                    },
                    "meetingId": {
                        "type": "string",
                        "description": "The meetingId / transcriptId to update privacy for"
                    }
                },
                "additionalProperties": false
            }
        },
        {
            "name": "fireflies_update_meeting_title",
            "cacheable": false,
            "description": "Updates the title of a meeting transcript. The authenticated user must be the owner of the meeting or a team admin. The title must be between 5 and 256 characters.",
            "ttl_seconds": null,
            "input_schema": {
                "type": "object",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "required": [
                    "meetingId",
                    "title"
                ],
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The new title for the meeting (5-256 characters)"
                    },
                    "meetingId": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The meeting ID / transcript ID to update"
                    }
                },
                "additionalProperties": false
            }
        }
    ],
    "secrets": [
        {
            "help": "Fireflies → Settings → Developer Settings → copy your API key.",
            "link": "https://docs.fireflies.ai/getting-started/mcp-configuration",
            "name": "FIREFLIES_API_KEY",
            "label": "Fireflies API Key",
            "secret": true,
            "env_var": "FIREFLIES_API_KEY",
            "required": true
        }
    ],
    "surface": {
        "n_tools": 20,
        "n_resources": 0
    },
    "operators": [
        "fireflies_create_soundbite",
        "fireflies_fetch",
        "fireflies_get_active_meetings",
        "fireflies_get_analytics",
        "fireflies_get_channel",
        "fireflies_get_rule_executions",
        "fireflies_get_soundbites",
        "fireflies_get_summary",
        "fireflies_get_transcript",
        "fireflies_get_transcripts",
        "fireflies_get_user",
        "fireflies_get_user_contacts",
        "fireflies_get_usergroups",
        "fireflies_list_channels",
        "fireflies_move_meeting",
        "fireflies_revoke_meeting_access",
        "fireflies_search",
        "fireflies_share_meeting",
        "fireflies_update_meeting_privacy",
        "fireflies_update_meeting_title"
    ],
    "resources": [
    ],
    "connection": {
        "env": {
            "FIREFLIES_API_KEY": "${FIREFLIES_API_KEY}"
        },
        "args": [
            "-y",
            "mcp-remote",
            "https://api.fireflies.ai/mcp",
            "--header",
            "Authorization: Bearer ${FIREFLIES_API_KEY}"
        ],
        "command": "npx",
        "transport": "stdio",
        "timeout_ms": 120000
    },
    "description": "Fireflies.ai meeting transcripts & summaries via the remote MCP (npx mcp-remote → https://api.fireflies.ai/mcp). Bearer FIREFLIES_API_KEY injected into the --header arg by the gateway; only the secret reference is stored in PG."
}
$cm$::jsonb,
    catalog_source => 'community',
    entry_active => true);
