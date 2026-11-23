"""Generate the contract golden files (Linear ENG-313).

``python -m tests.contract.generate`` rewrites ``tests/contract/golden``;
``tests/test_contract_golden.py`` regenerates every file in memory and
compares it byte for byte.

``python -m tests.contract.generate --out DIR --as-found`` writes the whole
set as this interpreter, its SQLite, its Unicode data and this platform
make it, into DIR, with a manifest (``platform.json``) saying what made it
and how each file compares with the committed one. It refuses nothing: a
generator that raises is recorded in the manifest and the run goes on. The
native build's tests read such a directory through ``BM_GOLDEN_DIR``
(native/README.md, "Parity"). Nothing here reads the network, the clock or a
random source in a way that reaches a golden byte.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import sys
import time
import traceback
import unicodedata
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import Context, copy_context
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import RLock

from . import (
    gen_argparse,
    gen_attribution,
    gen_hook_leaf,
    gen_caller_admission,
    gen_capture_vocabulary,
    gen_checkout_token,
    gen_checkout_token_read,
    gen_checkout_token_write,
    gen_checkout_token_remint,
    gen_cli,
    gen_cli_common,
    gen_cli_root,
    gen_config,
    gen_difflib,
    gen_credentials_prefixed,
    gen_credentials_gate,
    gen_credentials_spans,
    gen_builder_schema,
    gen_identity_caller,
    gen_identity_roots,
    gen_session,
    gen_standing_guard,
    gen_daemon,
    gen_edges,
    gen_fold,
    gen_store_event_reads,
    gen_follow,
    gen_discovery,
    gen_fs_realpath,
    gen_fs_access,
    gen_fs_samestat,
    gen_gitdir,
    gen_identity,
    gen_trust,
    gen_fs_stat,
    gen_fsutil,
    gen_gitconfig,
    gen_location_stamps,
    gen_location_reads,
    gen_gitconfig_file,
    gen_gitconfig_legacy,
    gen_gitconfig_tree,
    gen_gitconfig_remote_rules,
    gen_origin_gitfiles,
    gen_origin_gitfiles_attrs,
    gen_origin_gitfiles_facts,
    gen_origin_gitfiles_machine,
    gen_origin_gitfiles_root,
    gen_origin_gitfiles_walk,
    gen_gitconfig_wildmatch,
    gen_origin_remote,
    gen_owner_prompt,
    gen_location_remotes,
    gen_location_spellings,
    gen_location_stamped,
    gen_repository_history,
    gen_location_git_readers,
    gen_location_view,
    gen_location_answer,
    gen_location_witness,
    gen_location_discovery,
    gen_location_leaves,
    gen_location_cache,
    gen_location_capture,
    gen_gitsystem_answers,
    gen_gitsystem_ask,
    gen_gitsystem_sources,
    gen_handler_checks,
    gen_git_read_budget,
    gen_git_run,
    gen_gitsystem_probe,
    gen_gitsystem_record,
    gen_githead,
    gen_githead_discovery,
    gen_githead_signature,
    gen_location_probe,
    gen_ntrealpath,
    gen_location_caller_state,
    gen_location_resolve,
    gen_location_hook,
    gen_gitobjects,
    gen_gitobjects_files,
    gen_gitobjects_reader,
    gen_gitobjects_streams,
    gen_gitfile_leaves,
    gen_hook_stop,
    gen_hook_settle,
    gen_json,
    gen_native_text,
    gen_daemon_client,
    gen_install_hints,
    gen_in_force,
    gen_health_roots,
    gen_health_summary,
    gen_lexicon,
    gen_verify_candidates,
    gen_path_drift_steps,
    gen_policy,
    gen_attestation_paths,
    gen_project_file,
    gen_project_walk,
    gen_log,
    gen_log_keyfiles,
    gen_machine,
    gen_log_ring,
    gen_mcp,
    gen_membership_lines,
    gen_membership_migrate_report,
    gen_membership_queries,
    gen_membership_legacy,
    gen_claims,
    gen_claim_drift,
    gen_symbols,
    gen_pysyntax,
    gen_pysyntax_literal,
    gen_pysyntax_module,
    gen_models,
    gen_origin_history,
    gen_origin_cache,
    gen_origin_dates_reads,
    gen_origin_dates_memo,
    gen_origin_history_located,
    gen_origin_model,
    gen_restore_lock,
    gen_store_refusal,
    gen_state,
    gen_search_ranker,
    gen_search_dedup,
    gen_search_snippet,
    gen_store,
    gen_store_docfreq,
    gen_store_schema,
    gen_store_journal,
    gen_store_upgrade,
    gen_membership_records,
    gen_membership_shared,
    gen_membership_shared_ask,
    gen_membership_shared_heal,
    gen_membership_rows,
    gen_membership_tally,
    gen_membership_rung,
    gen_membership_core,
    gen_declare,
    gen_membership_views,
    gen_membership_writes,
    gen_membership_proposals,
    gen_membership_touch,
    gen_store_create,
    gen_store_digest_query,
    gen_store_files,
    gen_store_open,
    gen_store_records,
    gen_store_reads_seams,
    gen_store_records_b,
    gen_store_records_c,
    gen_store_records_d,
    gen_store_records_e,
    gen_store_returned,
    gen_store_record_event,
    gen_signed_key_sort,
    gen_store_membership,
    gen_store_rotation,
    gen_history_rows,
    gen_history_detail,
    gen_store_reads,
    gen_store_rows,
    gen_store_rows_written,
    gen_store_tx,
    gen_store_write,
    gen_store_path,
    gen_time,
    gen_transcript,
    gen_toml,
    gen_tool_handlers,
    gen_event_hook,
    gen_event_recorder,
    gen_events_kinds,
    gen_event_redact,
    gen_audit,
    gen_recall_settle,
    gen_recall_prompt,
    gen_control_tokens,
    gen_recall_render,
    gen_recall_choose,
    gen_ulid,
    gen_durability,
    gen_unicode,
    gen_unicode_xid,
    gen_use_tokens,
    gen_pyre,
    gen_pyre_lower,
    gen_pyre_exec,
    gen_pyre_api,
    gen_pyre_inventory,
    gen_models_text,
    gen_models_hits,
    gen_standing,
    gen_review_tables,
    gen_review_answers,
    gen_review_decision,
    gen_standing_reads,
    gen_mirror_format,
    gen_verify_status,
    gen_response_write,
    gen_response_hits,
    gen_response_reason,
    gen_verdict,
    gen_response_render,
    gen_handlers_verify,
    gen_witness_questions,
    gen_verify_anchoring_sparse,
    gen_verify_anchoring_anchor,
    gen_verify_anchoring_attest,
    gen_verify_anchoring_detect,
    gen_verify_anchoring_passes,
    gen_response_leaf,
    gen_verify_basis,
    gen_verify_weak,
    gen_verify_count,
    gen_verify_hit,
    gen_store_provenance,
    gen_store_bulk,
)

GOLDEN = Path(__file__).parent / "golden"

GENERATORS: dict[str, Callable[[], dict[str, bytes]]] = {
    "lexicon": gen_lexicon.generate,
    "verify_candidates": gen_verify_candidates.generate,
    "verify_candidates_scan": gen_verify_candidates.generate_scan,
    "verify_candidates_scan_unicode": gen_verify_candidates.generate_scan_unicode,
    "path_drift_steps": gen_path_drift_steps.generate,
    "policy": gen_policy.generate,
    "verify_candidates_unicode": gen_verify_candidates.generate_unicode,
    "attestation_paths": gen_attestation_paths.generate,
    "attestation_paths_posix": gen_attestation_paths.generate_posix,
    "project_file": gen_project_file.generate,
    "project_walk": gen_project_walk.generate,
    "project_file_parse": gen_project_file.generate_parse,
    "project_file_parse_python": gen_project_file.generate_parse_python,
    "project_file_parse_posix": gen_project_file.generate_parse_posix,
    "verify_candidates_patterns": gen_verify_candidates.generate_patterns,
    "verify_candidates_patterns_unicode": gen_verify_candidates.generate_patterns_unicode,
    "canonical_json": gen_json.generate,
    "native_text": gen_native_text.generate,
    "daemon_client": gen_daemon_client.generate,
    "install_hints": gen_install_hints.generate,
    "in_force": gen_in_force.generate,
    "health_roots": gen_health_roots.generate,
    "health_summary": gen_health_summary.generate,
    "log_chain": gen_log.generate,
    "log_keyfiles": gen_log_keyfiles.generate,
    "log_keyfiles_posix": gen_log_keyfiles.generate_posix,
    "log_keyfiles_posix_python": gen_log_keyfiles.generate_posix_python,
    "log_keyfiles_darwin": gen_log_keyfiles.generate_darwin,
    "machine_identity": gen_machine.generate,
    "machine_identity_python": gen_machine.generate_python,
    "machine_facts": gen_machine.generate_facts,
    "machine_facts_python": gen_machine.generate_facts_python,
    "machine_facts_unicode": gen_machine.generate_facts_unicode,
    "machine_facts_platform": gen_machine.generate_facts_platform,
    "machine_open": gen_machine.generate_open,
    "machine_open_platform": gen_machine.generate_open_platform,
    "log_keyring": gen_log_ring.generate,
    "store": gen_store.generate,
    "store_docfreq": gen_store_docfreq.generate,
    "store_schema_steps": gen_store_schema.generate,
    "store_schema_posix": gen_store_schema.generate_posix,
    "store_journal": gen_store_journal.generate,
    "store_upgrade": gen_store_upgrade.generate,
    "store_upgrade_posix": gen_store_upgrade.generate_posix,
    "membership_records_paths": gen_membership_records.generate,
    "membership_records_paths_python": gen_membership_records.generate_python,
    "membership_records_paths_platform": gen_membership_records.generate_platform,
    "membership_records_records": gen_membership_records.generate_records,
    "membership_records_identity": gen_membership_records.generate_identity,
    "membership_records_identity_python": gen_membership_records.generate_identity_python,
    "membership_shared": gen_membership_shared.generate,
    "membership_shared_python": gen_membership_shared.generate_python,
    "membership_shared_ask": gen_membership_shared_ask.generate,
    "membership_shared_heal": gen_membership_shared_heal.generate,
    "membership_rows": gen_membership_rows.generate,
    "membership_tally": gen_membership_tally.generate,
    "membership_rows_python": gen_membership_rows.generate_python,
    "membership_rung": gen_membership_rung.generate,
    "membership_core": gen_membership_core.generate,
    "membership_core_unicode": gen_membership_core.generate_unicode,
    "declare": gen_declare.generate,
    "membership_views": gen_membership_views.generate,
    "location_continuity": gen_membership_views.generate_continuity,
    "membership_writes": gen_membership_writes.generate,
    "membership_proposals": gen_membership_proposals.generate,
    "membership_touch": gen_membership_touch.generate,
    "search_ranker": gen_search_ranker.generate,
    "search_dedup": gen_search_dedup.generate,
    "search_dedup_unicode": gen_search_dedup.generate_unicode,
    "search_snippet": gen_search_snippet.generate,
    "search_snippet_unicode": gen_search_snippet.generate_unicode,
    "store_path": gen_store_path.generate,
    "datetime": gen_time.generate,
    "argparse": gen_argparse.generate,
    "argparse_python": gen_argparse.generate_python,
    "argparse_subparsers": gen_argparse.generate_subparsers,
    "argparse_subparsers_python": gen_argparse.generate_subparsers_python,
    "transcript": gen_transcript.generate,
    "transcript_python": gen_transcript.generate_python,
    "transcript_turns": gen_transcript.generate_turns,
    "origin_history": gen_origin_history.generate,
    "origin_cache": gen_origin_cache.generate,
    "origin_cache_posix": gen_origin_cache.generate_posix,
    "origin_dates_reads": gen_origin_dates_reads.generate,
    "origin_dates_memo": gen_origin_dates_memo.generate,
    "origin_history_located": gen_origin_history_located.generate,
    "origin_history_cuts": gen_origin_history.generate_cuts,
    "origin_model": gen_origin_model.generate,
    "unicode": gen_unicode.generate,
    "use_tokens": gen_use_tokens.generate,
    "use_tokens_python": gen_use_tokens.generate_python,
    "models_text": gen_models_text.generate,
    "models_text_unicode": gen_models_text.generate_unicode,
    "models_text_platform": gen_models_text.generate_platform,
    "models_hits": gen_models_hits.generate,
    "standing": gen_standing.generate,
    "review_tables": gen_review_tables.generate,
    "review_tables_unicode": gen_review_tables.generate_unicode,
    "review_answers": gen_review_answers.generate,
    "review_decision": gen_review_decision.generate,
    "standing_reads": gen_standing_reads.generate,
    "mirror_format": gen_mirror_format.generate,
    "unicode_sequences": gen_unicode.sequences,
    "unicode_xid": gen_unicode_xid.generate,
    "pyre": gen_pyre.generate,
    "pyre_unicode": gen_pyre.generate_unicode,
    "pyre_python": gen_pyre.generate_python,
    "pyre_lower": gen_pyre_lower.generate,
    "pyre_exec": gen_pyre_exec.generate,
    "pyre_api": gen_pyre_api.generate,
    "pyre_inventory": gen_pyre_inventory.generate,
    "python_edges": gen_edges.generate,
    "mcp_surface": gen_mcp.surface,
    "mcp_plain": gen_mcp.plain,
    "mcp": gen_mcp.responses,
    "daemon_http": gen_daemon.http_api,
    "hooks": gen_daemon.hooks,
    "hook_stop": gen_hook_stop.generate,
    "hook_settle": gen_hook_settle.generate,
    "settings": gen_config.generate,
    "settings_python": gen_config.generate_python,
    "settings_paths": gen_config.generate_paths,
    "settings_paths_python": gen_config.generate_paths_python,
    "difflib": gen_difflib.generate,
    "settings_create": gen_config.generate_create,
    "settings_create_posix": gen_config.generate_create_posix,
    "identity_caller": gen_identity_caller.generate,
    "identity_caller_unicode": gen_identity_caller.generate_unicode,
    "identity_roots": gen_identity_roots.generate,
    "identity_roots_python": gen_identity_roots.generate_python,
    "session": gen_session.generate,
    "standing_guard": gen_standing_guard.generate,
    "cli_surface": gen_cli.generate,
    "cli_common": gen_cli_common.generate,
    "cli_common_python": gen_cli_common.generate_python,
    "cli_root": gen_cli_root.generate,
    "cli_root_python": gen_cli_root.generate_python,
    "credentials_prefixed": gen_credentials_prefixed.generate,
    "credentials_prefixed_unicode": gen_credentials_prefixed.generate_unicode,
    "credentials_gate": gen_credentials_gate.generate,
    "credentials_gate_unicode": gen_credentials_gate.generate_unicode,
    "credentials_spans": gen_credentials_spans.generate,
    "credentials_spans_unicode": gen_credentials_spans.generate_unicode,
    "builder_schema": gen_builder_schema.generate,
    "state_files": gen_state.generate,
    "store_files": gen_store_files.generate,
    "models": gen_models.generate,
    "models_rows": gen_models.rows,
    "models_stores": gen_models.stores,
    "store_restore_lock": gen_restore_lock.generate,
    "store_restore_lock_hold": gen_restore_lock.generate_hold,
    "store_restore_lock_python": gen_restore_lock.generate_python,
    "store_restore_lock_hold_python": gen_restore_lock.generate_hold_python,
    "store_refusal": gen_store_refusal.generate,
    "store_refusal_posix": gen_store_refusal.generate_posix,
    "toml": gen_toml.generate,
    "tool_handlers": gen_tool_handlers.generate,
    "events_kinds": gen_events_kinds.generate,
    "event_redact": gen_event_redact.generate,
    "event_hook": gen_event_hook.generate,
    "event_recorder": gen_event_recorder.generate,
    "event_recorder_python": gen_event_recorder.generate_python,
    "audit_fields": gen_audit.generate,
    "audit_probe": gen_audit.generate_probe,
    "recall_settle": gen_recall_settle.generate,
    "recall_prompt": gen_recall_prompt.generate,
    "control_tokens": gen_control_tokens.generate,
    "control_tokens_unicode": gen_control_tokens.generate_unicode,
    "recall_render": gen_recall_render.generate,
    "recall_render_unicode": gen_recall_render.generate_unicode,
    "recall_choose": gen_recall_choose.generate,
    "fold": gen_fold.generate,
    "fold_python": gen_fold.generate_python,
    "store_event_reads": gen_store_event_reads.generate,
    "store_event_reads_naive": gen_store_event_reads.generate_naive,
    "store_create": gen_store_create.generate,
    "store_digest_query": gen_store_digest_query.generate,
    "store_digest_query_sqlite": gen_store_digest_query.generate_sqlite,
    "store_open": gen_store_open.generate,
    "store_write": gen_store_write.generate,
    "store_tx": gen_store_tx.generate,
    "store_records": gen_store_records.generate,
    "store_reads_seams": gen_store_reads_seams.generate,
    "store_reads_seams_python": gen_store_reads_seams.generate_python,
    "store_reads_seams_platform": gen_store_reads_seams.generate_platform,
    "store_records_b": gen_store_records_b.generate,
    "store_records_c": gen_store_records_c.generate,
    "store_records_d": gen_store_records_d.generate,
    "store_records_e": gen_store_records_e.generate,
    "store_returned": gen_store_returned.generate,
    "store_returned_posix": gen_store_returned.generate_posix,
    "store_record_event_posix": gen_store_record_event.generate_posix,
    "signed_key_sort_posix_python": gen_signed_key_sort.generate,
    "store_membership": gen_store_membership.generate,
    "store_rotation": gen_store_rotation.generate,
    "store_rotation_python": gen_store_rotation.generate_python,
    "history_rows": gen_history_rows.generate,
    "history_rows_python": gen_history_rows.generate_python,
    "store_reads": gen_store_reads.generate,
    "store_reads_python": gen_store_reads.generate_python,
    "store_rows_leaves": gen_store_rows.generate,
    "store_rows_leaves_python": gen_store_rows.generate_python,
    "store_rows_columns": gen_store_rows.generate_columns,
    "history_detail": gen_history_detail.generate,
    "history_rows_mac": gen_history_rows.generate_mac,
    "store_rows_written": gen_store_rows_written.generate,
    "gitconfig": gen_gitconfig.generate,
    "location_stamps": gen_location_stamps.generate,
    "location_stamps_python": gen_location_stamps.generate_python,
    "location_stamps_platform": gen_location_stamps.generate_platform,
    "location_reads": gen_location_reads.generate,
    "attribution": gen_attribution.generate,
    "attribution_unicode": gen_attribution.generate_unicode,
    "hook_leaf": gen_hook_leaf.generate,
    "caller_admission": gen_caller_admission.generate,
    "gitconfig_wildmatch": gen_gitconfig_wildmatch.generate,
    "origin_remote": gen_origin_remote.generate,
    "origin_remote_unicode": gen_origin_remote.generate_unicode,
    "origin_remote_python": gen_origin_remote.generate_python,
    "owner_prompt": gen_owner_prompt.generate,
    "gitconfig_remote_rules": gen_gitconfig_remote_rules.generate,
    "origin_gitfiles_leaves": gen_origin_gitfiles.generate,
    "origin_gitfiles_leaves_python": gen_origin_gitfiles.generate_python,
    "origin_gitfiles_machine": gen_origin_gitfiles_machine.generate,
    "origin_gitfiles_machine_python": gen_origin_gitfiles_machine.generate_python,
    "origin_gitfiles_darwin": gen_origin_gitfiles_machine.generate_darwin,
    "origin_gitfiles_facts": gen_origin_gitfiles_facts.generate,
    "origin_gitfiles_facts_posix": gen_origin_gitfiles_facts.generate_posix,
    "origin_gitfiles_root_posix": gen_origin_gitfiles_root.generate_posix,
    "origin_gitfiles_attrs_posix": gen_origin_gitfiles_attrs.generate_posix,
    "origin_gitfiles_walk_posix": gen_origin_gitfiles_walk.generate_posix,
    "ulid": gen_ulid.generate,
    "durability": gen_durability.generate,
    "durability_unicode": gen_durability.generate_unicode,
    "verify_status": gen_verify_status.generate,
    "response_write": gen_response_write.generate,
    "response_write_posix": gen_response_write.generate_posix,
    "response_hits": gen_response_hits.generate,
    "response_reason": gen_response_reason.generate,
    "verdict": gen_verdict.generate,
    "response_render": gen_response_render.generate,
    "handlers_verify_args": gen_handlers_verify.generate_args,
    "handlers_verify_args_unicode": gen_handlers_verify.generate_args_unicode,
    "handlers_verify_gates": gen_handlers_verify.generate_gates,
    "handlers_verify_gates_python": gen_handlers_verify.generate_gates_python,
    "witness_questions": gen_witness_questions.generate,
    "verify_anchoring_sparse": gen_verify_anchoring_sparse.generate,
    "verify_anchoring_anchor": gen_verify_anchoring_anchor.generate,
    "verify_anchoring_attest": gen_verify_anchoring_attest.generate,
    "verify_anchoring_attest_posix": gen_verify_anchoring_attest.generate_posix,
    "verify_anchoring_attest_unicode": gen_verify_anchoring_attest.generate_unicode,
    "verify_anchoring_detect": gen_verify_anchoring_detect.generate,
    "verify_anchoring_passes": gen_verify_anchoring_passes.generate,
    "verify_anchoring_passes_unicode": gen_verify_anchoring_passes.generate_unicode,
    "response_leaf": gen_response_leaf.generate,
    "verify_basis": gen_verify_basis.generate,
    "verify_weak": gen_verify_weak.generate,
    "verify_count": gen_verify_count.generate,
    "verify_hit": gen_verify_hit.generate,
    "verify_anchoring_skip": gen_verify_anchoring_sparse.generate_skip,
    "githead": gen_githead.generate,
    "githead_discovery": gen_githead_discovery.generate,
    "githead_signature": gen_githead_signature.generate,
    "location_probe": gen_location_probe.generate,
    "location_probe_python": gen_location_probe.generate_python,
    "location_probe_errno": gen_location_probe.generate_errno,
    "ntrealpath_python": gen_ntrealpath.generate_python,
    "location_caller_state": gen_location_caller_state.generate,
    "location_resolve": gen_location_resolve.generate,
    "location_hook": gen_location_hook.generate,
    "gitobjects": gen_gitobjects.generate,
    "gitobjects_python": gen_gitobjects.generate_python,
    "gitobjects_files": gen_gitobjects_files.generate,
    "gitobjects_files_posix": gen_gitobjects_files.generate_posix,
    "gitobjects_reader": gen_gitobjects_reader.generate,
    "gitobjects_reader_zlib": gen_gitobjects_reader.generate_zlib,
    "gitobjects_reader_posix": gen_gitobjects_reader.generate_posix,
    "gitobjects_streams": gen_gitobjects_streams.generate,
    "githead_posix": gen_githead.generate_posix,
    "capture_vocabulary": gen_capture_vocabulary.generate,
    "capture_vocabulary_python": gen_capture_vocabulary.generate_python,
    "checkout_token": gen_checkout_token.generate,
    "checkout_token_read": gen_checkout_token_read.generate,
    "checkout_token_read_posix": gen_checkout_token_read.generate_posix,
    "checkout_token_write": gen_checkout_token_write.generate,
    "checkout_token_write_posix": gen_checkout_token_write.generate_posix,
    "checkout_token_remint": gen_checkout_token_remint.generate,
    "checkout_token_remint_posix": gen_checkout_token_remint.generate_posix,
    "gitconfig_file": gen_gitconfig_file.generate,
    "gitconfig_tree": gen_gitconfig_tree.generate,
    "gitconfig_tree_rules": gen_gitconfig_tree.generate_rules,
    "gitconfig_legacy": gen_gitconfig_legacy.generate,
    "location_remotes": gen_location_remotes.generate,
    "location_spellings": gen_location_spellings.generate,
    "location_stamped": gen_location_stamped.generate,
    "repository_history": gen_repository_history.generate,
    "location_git_readers": gen_location_git_readers.generate,
    "location_git_readers_posix": gen_location_git_readers.generate_posix,
    "location_view": gen_location_view.generate,
    "location_view_posix": gen_location_view.generate_posix,
    "location_answer": gen_location_answer.generate,
    "location_witness": gen_location_witness.generate,
    "location_discovery": gen_location_discovery.generate,
    "location_leaves": gen_location_leaves.generate,
    "location_leaves_python": gen_location_leaves.generate_python,
    "location_leaves_posix": gen_location_leaves.generate_posix,
    "location_cache": gen_location_cache.generate,
    "location_cache_posix": gen_location_cache.generate_posix,
    "location_capture": gen_location_capture.generate,
    "location_capture_platform": gen_location_capture.generate_platform,
    "gitsystem_record": gen_gitsystem_record.generate,
    "gitsystem_record_python": gen_gitsystem_record.generate_python,
    "gitsystem_answers": gen_gitsystem_answers.generate,
    "gitsystem_answers_unicode": gen_gitsystem_answers.generate_unicode,
    "gitsystem_answers_python": gen_gitsystem_answers.generate_python,
    "gitsystem_probe": gen_gitsystem_probe.generate,
    "gitsystem_ask": gen_gitsystem_ask.generate,
    "gitsystem_ask_unicode": gen_gitsystem_ask.generate_unicode,
    "gitsystem_ask_python": gen_gitsystem_ask.generate_python,
    "gitsystem_which_windows": gen_gitsystem_ask.generate_which_windows,
    "gitsystem_sources": gen_gitsystem_sources.generate,
    "handler_checks": gen_handler_checks.generate,
    "git_read_budget": gen_git_read_budget.generate,
    "git_run": gen_git_run.generate,
    "gitsystem_sources_python": gen_gitsystem_sources.generate_python,
    "gitsystem_which_windows_python": (gen_gitsystem_ask.generate_which_windows_python),
    "gitsystem_probe_python": gen_gitsystem_probe.generate_python,
    "gitsystem_probe_names": gen_gitsystem_probe.generate_names,
    "gitsystem_probe_names_unicode": gen_gitsystem_probe.generate_names_unicode,
    "gitconfig_legacy_python": gen_gitconfig_legacy.generate_python,
    "membership_lines": gen_membership_lines.generate,
    "membership_lines_unicode": gen_membership_lines.generate_unicode,
    "membership_lines_python": gen_membership_lines.generate_python,
    "membership_migrate_report": gen_membership_migrate_report.generate,
    "membership_migrate_report_python": gen_membership_migrate_report.generate_python,
    "membership_queries": gen_membership_queries.generate,
    "membership_legacy": gen_membership_legacy.generate,
    "claims": gen_claims.generate,
    "claims_unicode": gen_claims.generate_unicode,
    "claims_python": gen_claims.generate_python,
    "claims_literal": gen_claims.generate_literal,
    "claim_drift": gen_claim_drift.generate,
    "claim_drift_unicode": gen_claim_drift.generate_unicode,
    "claim_drift_python": gen_claim_drift.generate_python,
    "symbols": gen_symbols.generate,
    "symbols_platform": gen_symbols.generate_platform,
    "pysyntax": gen_pysyntax.generate_stable,
    "pysyntax_python": gen_pysyntax.generate,
    "pysyntax_literal": gen_pysyntax_literal.generate_stable,
    "pysyntax_literal_python": gen_pysyntax_literal.generate,
    "pysyntax_module": gen_pysyntax_module.generate,
    "gitfile_leaves": gen_gitfile_leaves.generate,
    "gitfile_leaves_posix": gen_gitfile_leaves.generate_posix,
    "store_provenance": gen_store_provenance.generate,
    "fs_realpath": gen_fs_realpath.generate,
    "fs_realpath_python": gen_fs_realpath.generate_python,
    "store_bulk": gen_store_bulk.generate,
    "fs_stat": gen_fs_stat.generate,
    "errno_class": gen_fs_stat.generate_errno_class,
    "fs_access": gen_fs_access.generate,
    "fs_realpath_strict": gen_fs_realpath.generate_strict,
    "fs_realpath_strict_python": gen_fs_realpath.generate_strict_python,
    "fs_samestat": gen_fs_samestat.generate,
    "fsutil": gen_fsutil.generate,
    "fsutil_python": gen_fsutil.generate_python,
    "fsutil_flock": gen_fsutil.generate_flock,
    "fsutil_flock_python": gen_fsutil.generate_flock_python,
    "fsutil_flock_platform": gen_fsutil.generate_flock_platform,
    "gitdir_read": gen_gitdir.generate,
    "gitdir_read_python": gen_gitdir.generate_python,
    "gitfile_identity": gen_identity.generate,
    "gitfile_identity_python": gen_identity.generate_python,
    "gitfile_trust": gen_trust.generate,
    "gitfile_trust_python": gen_trust.generate_python,
    "gitfile_discovery": gen_discovery.generate,
    "gitfile_discovery_python": gen_discovery.generate_python,
    "gitfile_pin_text": gen_follow.generate_text,
    "gitfile_pin_text_python": gen_follow.generate_text_python,
    "gitfile_follow": gen_follow.generate,
    "gitfile_follow_python": gen_follow.generate_python,
}

#: The Unicode data version the golden files were generated under. The
#: generators gated on it read `unicodedata` or `re`'s word class for
#: their output; under another version they are not compared.
UNIDATA_VERSION = "15.1.0"
UNICODE_GATED = frozenset(
    {
        "attribution_unicode",
        # `find_control_tokens` over notes and attestations past ASCII.
        "handlers_verify_args_unicode",
        "lexicon",
        "verify_candidates_unicode",
        "verify_anchoring_attest_unicode",
        "verify_anchoring_passes_unicode",
        "verify_candidates_scan_unicode",
        "verify_candidates_patterns_unicode",
        "unicode",
        "models_text_unicode",
        "unicode_sequences",
        "unicode_xid",
        "pyre_unicode",
        "store",
        "store_path",
        "store_files",
        "search_ranker",
        "search_dedup_unicode",
        "search_snippet_unicode",
        # `\s` and the tokenizer past ASCII (`gen_review_tables`).
        "review_tables_unicode",
        "mcp",
        "hooks",
        "credentials_prefixed_unicode",
        "credentials_gate_unicode",
        # `re`'s `\s`, `\b` and IGNORECASE past ASCII in the generic and
        # URI rules and the snippet's `\s+` (`gen_credentials_spans`).
        "credentials_spans_unicode",
        "control_tokens_unicode",
        "recall_render_unicode",
        "machine_facts_unicode",
        "durability_unicode",
        "fold",
        "fold_python",
        "store_create",
        "store_open",
        "store_write",
        "store_tx",
        "store_records",
        "store_records_b",
        "store_records_c",
        "store_records_d",
        "store_records_e",
        "store_returned_posix",
        "store_membership",
        "identity_caller_unicode",
        "membership_lines_unicode",
        "membership_core_unicode",
        "origin_remote_unicode",
        "origin_remote_python",
        "claims_unicode",
        "claim_drift_unicode",
        "store_rows_written",
        "store_provenance",
        "store_bulk",
        "gitsystem_record",
        "gitsystem_answers_unicode",
        "gitsystem_probe_names_unicode",
        "gitsystem_ask_unicode",
    }
)

#: The SQLite the golden files were generated under: the store fixture
#: holds FTS5's shadow-table definitions and ``bm25()`` scores. The tool
#: responses rank hits through FTS5 and tokenize through `re`'s word class,
#: so ``mcp`` is gated on both; so is ``hooks``, whose hooks rank the
#: prompt recall's pointers and the Stop audit's hits the same way, and
#: ``store_files``, whose FTS case records ``bm25()`` score bits.
#: ``store_rows_columns`` names the SQLite it read the schema's columns
#: under (FTS5's shadow tables among them). ``store_reads_python`` holds
#: busy timeouts past 32 bits, which SQLite 3.50.4 sets to 0.
SQLITE_VERSION = "3.50.4"
SQLITE_GATED = frozenset(
    {
        "store",
        "store_path",
        "store_files",
        "mcp",
        "hooks",
        "models_stores",
        "store_rows_columns",
        "store_reads_python",
        # The order of rows a query without ORDER BY returns.
        "store_digest_query_sqlite",
    }
)

#: The CPython the golden files were generated under, to the patch
#: release. ``python_edges`` records what changes between versions
#: (`datetime.fromisoformat`'s C parser, where `list.sort` leaves a NaN
#: key, the recursion budget JSON stops at); under another version it is
#: not compared. The patch release is part of it: CI installs the newest
#: 3.13 (3.13.18 on 2026-11-15) and its `datetime_edges.jsonl` differs
#: from 3.13.13's, so a minor-version gate compared files it should have
#: skipped (CI run 43752288601).
PYTHON_VERSION = "3.13.13"
#: ``models_rows`` and ``models_stores`` read row datetimes through
#: `datetime.fromisoformat`, whose grammar is the release's.
#: ``toml`` and ``settings_python`` / ``settings_paths_python`` hold the
#: interpreter's wording (`tomllib`'s messages, codec, `int()` and `repr`
#: texts, pathlib's parsing); 3.11 and 3.12 ship another `tomllib`.
#: ``store_rows_leaves_python`` holds log payloads nested near the
#: recursion budget `json.loads` stops at (`gen_store_rows`).
#: ``store_reads_python`` holds the name `sqlite_errorname` gives each
#: SQLite result code, from the release's table, and busy timeouts past
#: 32 bits of milliseconds, which the SQLite decides (`gen_store_reads`).
#: ``gitsystem_record_python`` holds records nested past 1,000 levels,
#: where `json.loads` raising RecursionError is the interpreter's.
#: ``gitsystem_answers_python`` holds Windows paths a separator then a colon
#: begins (3.11's `ntpath.splitdrive` reads a drive there) and digit runs
#: past `int`'s limit. ``gitsystem_probe_python`` checks links and folders
#: through a folder that cannot be searched, where 3.13's `Path.is_dir`
#: raises and 3.14's answers False. ``gitsystem_which_windows_python``
#: names ``NoDefaultCurrentDirectoryInExePath`` or a PATHEXT entry ending in
#: a dot, which `shutil.which` before 3.12 does not read as 3.13 does.
#: ``history_rows_python`` holds log payloads nested near `json.loads`'s
#: recursion budget (`gen_history_rows`). ``membership_shared_python``
#: holds log payloads nested 200,000 arrays deep, which `json.loads` reads
#: whole under 3.14 on Linux (`gen_membership_shared`).
#: ``store_reads_seams_python`` holds `Cursor.fetchmany` sizes past its
#: bounds, which 3.13 checks otherwise than 3.12 and words otherwise than
#: 3.14 (`gen_store_reads_seams`).
#: ``origin_remote_python`` holds URLs with a bracket, whose host checks in
#: `urlsplit` and `ipaddress` are the release's (`gen_origin_remote`).
#: ``gitfile_pin_text_python`` holds pin records read through ``ntpath``
#: whose path is rooted without a drive (``/g``): `ntpath.isabs` reads it
#: as absolute in 3.11 and 3.12 and as relative from 3.13 (`gen_follow`).
#: ``pyre_python`` holds the interpreter's own version, ``_sre.MAGIC`` and
#: the digests of the ``re`` sources the engine was read from (`gen_pyre`),
#: and patterns with an empty negative lookaround, which `re._parser`
#: parses to FAILURE from 3.13 (`gen_pyre_tree`).
#: ``cli_common_python`` holds cutoffs read by `datetime.fromisoformat`
#: at its edges (week dates, compact forms, offsets with seconds, a NUL,
#: digits past ASCII), whose answers follow the release (`gen_cli_common`).
#: ``cli_root_python`` holds the CLI root parser's exits (help, version,
#: errors) and argvs holding ``--``: `argparse`'s texts and readings
#: follow the release (`gen_cli_root`).
#: ``pysyntax_python`` holds the eval-mode tokenizer's and
#: `ast.literal_eval`'s answers as 3.13.13 gives them, classed by the
#: release's messages; under another interpreter the generator writes its
#: header alone. Its sibling ``pysyntax`` holds the values alone, which
#: 3.11 to 3.14 answer alike, and is not gated (`gen_pysyntax`).
#: ``pysyntax_literal_python`` holds the texts of mixed real and complex
#: arithmetic, whose values `ast.literal_eval` of 3.14 changed, as 3.13.13
#: answers them; under another interpreter the generator writes its
#: header alone. Its sibling ``pysyntax_literal`` holds every other text,
#: which 3.11 to 3.14 answer alike, and is not gated
#: (`gen_pysyntax_literal`).
#: ``pysyntax_module`` holds the module-mode tokenizer's answers as
#: 3.13.13 gives them, classed by the release's messages; under another
#: interpreter the generator writes its header alone
#: (`gen_pysyntax_module`).
#: ``location_stamps_platform`` holds `os.major` and `os.minor` of NODEV
#: (-1) and of numbers past ``dev_t``. 3.11 truncates a number macOS's
#: 32-bit ``dev_t`` cannot hold, where 3.12.13 and 3.13 raise; 3.11 and
#: 3.12.3 raise for NODEV, which 3.12.13 and 3.13 accept, and these answer
#: a part of 0xffffffff on glibc as -1 (`gen_location_stamps`).
#: ``location_stamps_python`` and ``origin_gitfiles_machine_python`` hold
#: the ValueError `os.stat` raises for a NUL in a path, "embedded null
#: byte" in 3.11.15 and "stat: embedded null character in path" in
#: 3.12.13, 3.13 and 3.14 (`gen_location_stamps`,
#: `gen_origin_gitfiles_machine`).
#: ``membership_records_paths_platform`` holds rows of the Python-gated
#: file as well as release-stable ones (`gen_membership_records`).
#: ``location_leaves_python`` holds `nturl2path.url2pathname`'s answers,
#: whose text changed within 3.13 and again in 3.14 (`gen_location_leaves`).
#: ``machine_facts_platform`` records which birth time attribute
#: `os.stat_result` carries, which follows the release and the platform
#: (`gen_machine`); so does ``machine_open_platform``, which opens a real
#: state directory and reads the id file's birth time.
#: ``location_probe_python`` probes link loops, which 3.11's `realpath`
#: walks otherwise, and paths holding a NUL, whose ValueError text 3.11
#: words otherwise (`gen_location_probe`).
#: ``ntrealpath_python`` holds `ntpath.realpath`'s Windows walk, which
#: 3.11, 3.12 and 3.13 each walk differently (`gen_ntrealpath`).
#: ``argparse_python`` holds `argparse`'s error texts, usage lines, help and
#: version output and its prefix, ``=``, cluster and ``--`` readings as
#: 3.13.13 gives them; 3.12 words the choices and wraps the usage line
#: otherwise, and ``--`` is read otherwise across releases (`gen_argparse`).
#: ``store_rotation_python`` finishes a rotation whose binding is nested
#: 100,000 arrays deep: `json.loads` raises RecursionError where the
#: interpreter stops recursing, which 3.14 on Linux does not reach
#: (`gen_store_rotation`).
#: ``argparse_subparsers_python`` is the same for subparsers: the invalid
#: choice text and the choice listing are worded otherwise before 3.12.
#: ``log_keyfiles_posix_python``, ``machine_facts_python`` and
#: ``fold_python`` hold the rows whose input is nested 20,000 or 100,000
#: levels, where `json.loads` raising RecursionError is the interpreter's
#: and the system's: 3.11 to 3.13 and 3.14 on macOS raise, 3.14 on Linux
#: measures the C stack and reads the input (CI run of 65aea201,
#: "test (ubuntu-latest, py3.14)"). Their siblings ``log_keyfiles_posix``,
#: ``machine_facts`` and ``fold`` hold every other row and are not gated
#: on the release (`gen_log_keyfiles`, `gen_machine`, `gen_fold`).
#: ``store_restore_lock_python`` holds the `admit` row of a marker nested
#: 70,000 lists deep, for the same reason (`gen_restore_lock`).
#: ``store_restore_lock_hold_python`` holds the hold row of a marker nested
#: 70,000 lists deep: whether `json.loads` raises RecursionError there is
#: the interpreter's and the system's, as 3.14 on Linux reads it
#: (`gen_restore_lock`).
#: ``symbols_platform`` holds a link loop, on which 3.11 and 3.12's
#: `Path.resolve` raises RuntimeError out of the checker (`gen_symbols`).
#: ``project_file_parse_python`` holds `tomllib`'s refusals, whose messages
#: and `int()` digit limit are the release's (`gen_project_file`).
PYTHON_GATED = frozenset(
    {
        "argparse_python",
        "argparse_subparsers_python",
        "cli_common_python",
        "cli_root_python",
        # 3.11 and 3.12 raise RuntimeError where `Path.resolve` meets a link loop.
        "handlers_verify_gates_python",
        "symbols_platform",
        "models_text_platform",
        "location_stamps_platform",
        "location_stamps_python",
        "origin_gitfiles_machine_python",
        "location_leaves_python",
        "gitsystem_record_python",
        "gitsystem_answers_python",
        "gitsystem_probe_python",
        "gitsystem_ask_python",
        "gitsystem_which_windows_python",
        "gitsystem_sources_python",
        "history_rows_python",
        "store_reads_python",
        "store_rows_leaves_python",
        "signed_key_sort_posix_python",
        "origin_gitfiles_leaves_python",
        "project_file_parse_python",
        "pyre_python",
        "python_edges",
        "models_rows",
        "models_stores",
        "toml",
        # The TypeError's text of an unhashable set element (3.14 words it
        # otherwise).
        "use_tokens_python",
        # Where the JSON decoder stops recursing on a line nested 20,000
        # deep follows the release and the system (`gen_transcript`).
        "transcript_python",
        "settings_python",
        "settings_paths_python",
        "membership_lines_python",
        # Where the JSON decoder stops recursing in `_origin` and
        # `_json_list` (`gen_membership_migrate_report`).
        "membership_migrate_report_python",
        "membership_records_paths_python",
        "membership_records_paths_platform",
        # `str.strip`, `str.lower` and `repr` of names and witnesses past
        # ASCII follow the release's Unicode tables (`gen_membership_records`).
        "membership_records_identity_python",
        "membership_rows_python",
        # CPython's text for a keyword passed twice names the callable as
        # the release spells it (`gen_event_recorder`).
        "event_recorder_python",
        "membership_shared_python",
        "origin_remote_python",
        "claims_python",
        "pysyntax_python",
        "pysyntax_literal_python",
        "pysyntax_module",
        "capture_vocabulary_python",
        "fs_realpath_python",
        "fs_realpath_strict_python",
        "fsutil_python",
        "fsutil_flock_python",
        "fsutil_flock_platform",
        "gitconfig_legacy_python",
        "gitdir_read_python",
        "gitfile_identity_python",
        # URIs with a bracket: the bracketed-host checks of `urlsplit` and
        # `ipaddress` are the release's (`gen_identity_roots`).
        "identity_roots_python",
        "gitfile_trust_python",
        "gitfile_follow_python",
        "gitfile_pin_text_python",
        "gitfile_discovery_python",
        "gitobjects_python",
        "machine_identity_python",
        "machine_facts_platform",
        "machine_open_platform",
        "store_reads_seams_python",
        "location_probe_python",
        "ntrealpath_python",
        "store_rotation_python",
        "log_keyfiles_posix_python",
        "machine_facts_python",
        "fold_python",
        "store_restore_lock_python",
        "store_restore_lock_hold_python",
    }
)

#: The zlib CPython links (`zlib.ZLIB_RUNTIME_VERSION`). ``gitobjects_streams``
#: pins what `zlib.decompressobj` takes of a stream and returns, and when
#: it raises: where that zlib's `inflate` stops and how much input it has
#: consumed there. The streams are written bit by bit, so no byte of them
#: is a compressor's, but the stopping points are the inflate's. macOS's
#: libz 1.2.12 and the native build's `zlib-rs` agree on them; no other
#: build has been compared, so under another the file is not compared.
#: ``gitobjects_reader_zlib`` pins the object reader over damaged streams,
#: whose answers follow from the same stopping points
#: (`gen_gitobjects_reader`).
ZLIB_VERSION = "1.2.12"
ZLIB_GATED = frozenset({"gitobjects_streams", "gitobjects_reader_zlib"})


def zlib_version() -> str:
    return zlib.ZLIB_RUNTIME_VERSION


#: The pydantic-core the record fixtures were generated under (uv.lock pins
#: pydantic 2.14.1 on it): the models' coercions and errors are its.
PYDANTIC_CORE_VERSION = "2.50.1"
PYDANTIC_GATED = frozenset(
    {
        "store_journal",
        "models",
        "models_rows",
        "models_stores",
        "fold",
        "fold_python",
        "store_write",
        "store_tx",
        "store_records",
        "store_reads_seams",
        "store_reads_seams_platform",
        "store_records_b",
        "store_records_c",
        "store_records_d",
        "store_records_e",
        "store_returned_posix",
        "store_membership",
        "store_rows_written",
        "store_provenance",
        "store_bulk",
    }
)


def pydantic_core_version() -> str:
    import pydantic_core

    return str(pydantic_core.__version__)


#: The platform the files that hold floating-point score bits were
#: generated on. A ranking's scores go through the platform's `log`,
#: `exp` and `log1p` and its compiler's double arithmetic, in Python and
#: in SQLite's ``bm25()``, and CI run 43759029312 showed 10 of
#: search_ranker.jsonl's 208 queries scoring other bits on Linux x86-64
#: under the same Python minor and Unicode. So bit-equality of scores is a
#: property of one platform: a build is held to the reference on the
#: platform it runs on, and elsewhere a difference is skipped and named.
#: ``location_stamps_platform`` makes the running platform's device names
#: and carries the others' as recorded (`gen_location_stamps`); on Windows
#: it adds a section of its own.
#: ``machine_facts_platform`` stats a real file: whether a birth time is
#: given, and as ``st_birthtime_ns`` or ``st_birthtime``, is the
#: platform's (`gen_machine`), and ``machine_open_platform`` reads it
#: through `open_machine`.
#: ``location_probe_errno`` raises every errno number, whose meaning and
#: whose text (the C library's `strerror`) are the platform's.
#: ``membership_records_paths_platform`` holds the cases whose reference
#: answer follows the host: on Windows `ntpath.normcase` lowers through
#: ``LCMapStringEx`` (U+0130 kept, no final sigma) and `ntpath.abspath`
#: reads the process's folder through ``_getfullpathname``, where on a
#: POSIX host both are Python (`gen_membership_records`).
#: ``fsutil_flock_platform`` holds pathlib's refusals of a lock path with
#: no name, whose text names the host's class: ``PosixPath('/')`` on a
#: POSIX host, ``WindowsPath('/')`` on Windows (`gen_fsutil`).
#: ``log_keyfiles_darwin`` holds `KeyUnreadableError` messages whose reason
#: is the C library's ``strerror`` (`gen_log_keyfiles`).
#: ``symbols_platform`` resolves cited modules through links and names
#: that differ only in case, which macOS folds and Linux does not
#: (`gen_symbols`).
#: ``location_capture_platform`` holds `_file_tracked` over tree modes wider
#: than 16 bits, which `stat.S_ISDIR` refuses where the C ``mode_t`` is 16
#: bits wide (macOS, Windows) and reads on Linux (`gen_location_capture`).
PLATFORM = "darwin-arm64"
PLATFORM_GATED = frozenset(
    {
        "symbols_platform",
        "models_text_platform",
        "location_stamps_platform",
        "log_keyfiles_darwin",
        "search_ranker",
        "store",
        "store_path",
        "store_files",
        "store_restore_lock",
        "store_restore_lock_hold",
        "store_restore_lock_python",
        "store_restore_lock_hold_python",
        "mcp",
        "origin_gitfiles_darwin",
        "hooks",
        "location_probe_errno",
        "location_capture_platform",
        "membership_records_paths_platform",
        "machine_facts_platform",
        "fsutil_flock_platform",
        "machine_open_platform",
        "store_reads_seams_platform",
    }
)


#: Generators with no case on Windows: ``store_restore_lock`` and
#: ``store_restore_lock_python`` drive a
#: POSIX directory ``flock``, as do ``store_restore_lock_hold`` and
#: ``store_restore_lock_hold_python`` (the lock there is a record file and a handle
#: on the store, ENG-321). Under ``--as-found`` on Windows they are named in
#: the manifest (``not_here``) and write no file; that is no raise, and the
#: golden test skips them there.
#: ``settings_paths`` and ``settings_paths_python`` expand, resolve and
#: print paths by POSIX rules (`~name`, the root, `//`, a NUL), which the
#: native tests' world reproduces; on Windows the reference's paths differ.
#: ``settings_create_posix`` reads a new config's mode bits and makes the
#: first-run write fail over a file parent and a mode-0600 parent
#: (`gen_config.generate_create_posix`), which Windows answers otherwise.
#: ``store_open`` reads modes and writes the key ring's binding as a
#: resolved POSIX path under a POSIX ``flock``; ``store_write`` records the
#: same state after each write, ``store_tx`` after each transaction,
#: ``store_records`` after each writer call, ``store_returned_posix`` after
#: each backfill step; ``store_record_event_posix`` opens such stores to
#: write events into them.
#: ``githead_posix`` reads links, FIFOs and ENOTDIR, which Windows answers
#: otherwise (`gen_githead`); ``githead_discovery`` walks trees with links,
#: FIFOs and modes and reads owners and devices through ``os.lstat`` and
#: ``os.stat`` (`gen_githead_discovery`); ``githead_signature`` stamps and
#: rewrites such trees between reads (`gen_githead_signature`);
#: ``origin_history_located`` reads
#: repositories through that discovery, and ``origin_history_located_modes``
#: through mode-000 files and folders (`gen_origin_history_located`); ``gitconfig_file`` reads links, FIFOs and
#: mode-000 files and opens directories (`gen_gitconfig_file`);
#: ``fs_realpath`` and ``fs_realpath_python`` resolve links and FIFOs by
#: ``posixpath`` (`gen_fs_realpath`); ``fs_stat`` states and lists links,
#: FIFOs and mode-000 folders (`gen_fs_stat`); ``errno_class`` names POSIX
#: errnos (`gen_fs_stat`); ``fs_access`` asks ``access`` and opens with
#: ``O_NOFOLLOW`` over links, FIFOs and modes (`gen_fs_access`);
#: ``fs_realpath_strict`` and ``fs_realpath_strict_python`` resolve the
#: same trees strictly; ``fs_samestat`` states pairs over links and hard
#: links (`gen_fs_samestat`); ``gitdir_read`` and ``gitdir_read_python``
#: read git directories over links, FIFOs and modes (`gen_gitdir`);
#: ``gitfile_identity`` and ``gitfile_identity_python`` state and list
#: trees with links, hard links and modes (`gen_identity`);
#: ``gitfile_trust`` and ``gitfile_trust_python`` read configs and
#: ``commondir`` files over links and modes (`gen_trust`);
#: ``gitfile_follow`` and ``gitfile_follow_python`` follow ``.git`` entries
#: over links, modes and pin records (`gen_follow`);
#: ``gitfile_discovery`` and ``gitfile_discovery_python`` judge ``.git``
#: entries and walk real trees with links, FIFOs and modes
#: (`gen_discovery`);
#: ``gitconfig_tree`` reads real trees with the host's ``os.path`` and ``pwd``;
#: ``gitconfig_legacy`` and ``gitconfig_legacy_python`` read real trees with
#: links, FIFOs and modes (`gen_gitconfig_legacy`; the second holds the link
#: loops); ``location_remotes`` and ``location_spellings`` read real trees
#: with modes, links and FIFOs (`gen_location_remotes`); ``location_stamped``
#: changes such trees between reads (`gen_location_stamped`);
#: ``gitobjects_files_posix`` walks alternates over links, FIFOs and modes,
#: its directories spelled by ``posixpath.realpath`` (`gen_gitobjects_files`);
#: ``location_leaves_posix`` resolves local remote keys and git paths
#: through links on disk (`gen_location_leaves`);
#: ``location_git_readers_posix`` walks refs and trees with links and mode-000
#: entries (`gen_location_git_readers`); ``location_view_posix`` builds
#: repositories with links and a mode-000 folder (`gen_location_view`);
#: ``gitobjects_reader_posix`` reads objects over FIFOs, modes and an
#: alternates file (`gen_gitobjects_reader`);
#: ``verify_anchoring_skip`` resolves working trees through links on disk
#: and spells paths by ``posixpath`` (`gen_verify_anchoring_sparse`);
#: ``location_discovery`` walks trees with links and spells paths with
#: ``/`` (`gen_location_discovery`); ``location_cache_posix`` stamps real
#: trees and reads a mode-000 HEAD (`gen_location_cache`);
#: ``gitsystem_record`` reads and writes the record over links, modes and
#: FIFOs (`gen_gitsystem_record`); ``gitsystem_probe`` builds, sweeps and
#: clears the probe over links, modes and FIFOs (`gen_gitsystem_probe`);
#: ``gitfile_leaves_posix`` reads links and mode-000 files
#: (`gen_gitfile_leaves`); ``checkout_token_read_posix`` reads links, a
#: FIFO and a loop (`gen_checkout_token_read`); ``checkout_token_read``
#: runs the reads over a scripted OS whose logged calls spell the path as
#: the host's ``Path`` does (backslashes on Windows) and whose errno names
#: are the host's ``errno`` module's (``WSAELOOP`` for ELOOP on Windows),
#: so its bytes follow the OS family (`gen_checkout_token_read`);
#: ``checkout_token_write`` runs `write` over the same scripted OS and
#: ``checkout_token_write_posix`` writes real trees with modes and real
#: errno texts (`gen_checkout_token_write`); ``checkout_token_remint`` and
#: ``checkout_token_remint_posix`` do the same for `remint`
#: (`gen_checkout_token_remint`);
#: ``verify_candidates_scan`` and ``verify_candidates_scan_unicode`` expand
#: ``~`` through the host's ``Path``, whose Windows flavour parses and
#: spells paths otherwise (`gen_verify_candidates`);
#: ``location_stamps`` and
#: ``location_stamps_python`` name devices by ``os.major``, which Windows'
#: ``os`` lacks (`gen_location_stamps`); ``location_probe``, ``location_probe_python``
#: and ``location_probe_errno`` probe trees with links and a mode-000
#: folder, the last raising POSIX errno numbers (`gen_location_probe`);
#: ``origin_gitfiles_facts_posix`` joins with the host's ``os.path``
#: (``ntpath`` on Windows) and holds trees with links and a mode-000
#: folder (`gen_origin_gitfiles_facts`); ``attestation_paths_posix`` reads
#: a tree with links and a mode-000 folder (`gen_attestation_paths`);
#: ``project_walk`` walks trees with links and a mode-000 folder, holds
#: strerror texts and spells paths as the host's ``Path`` does
#: (`gen_project_walk`);
#: ``store_schema_posix`` runs the schema steps on a mode-0444 store file
#: and spells a v8 folder's path as the host's ``Path`` does
#: (`gen_store_schema`);
#: ``store_upgrade_posix`` holds SQLite's file locks from a second
#: connection, seen on POSIX only (`gen_store_upgrade`);
#: ``log_keyfiles_posix``, ``log_keyfiles_posix_python`` and
#: ``log_keyfiles_darwin`` drive the key ring over links, mode-000 files
#: and folders (`gen_log_keyfiles`);
#: ``origin_gitfiles_root_posix`` makes links, a FIFO and a mode-000 file
#: and spells a root as the host's ``Path`` does (`gen_origin_gitfiles_root`).
#: ``origin_gitfiles_attrs_posix`` makes links, a FIFO and mode-000 entries
#: and discovers repositories through the owner's check, which declines off
#: POSIX (`gen_origin_gitfiles_attrs`).
#: ``origin_gitfiles_walk_posix`` discovers repositories through the owner's
#: check, which declines off POSIX (`gen_origin_gitfiles_walk`).
#: ``store_rotation`` and ``store_rotation_python`` finish key rotations
#: over links, hard links, mode-000 files and read-only folders
#: (`gen_store_rotation`).
#: ``machine_open`` and ``machine_open_platform`` record the POSIX modes
#: `open_machine` leaves on the state directory and its two files, which
#: the native build does not port to Windows (`gen_machine`);
#: ``verify_anchoring_attest_posix`` resolves through a link in a scratch
#: tree (`gen_verify_anchoring_attest`);
#: ``verify_anchoring_detect`` spells candidates through the host's `Path`
#: and locates repositories through `os.path.realpath`
#: (`gen_verify_anchoring_detect`);
#: ``symbols_platform`` holds links, a FIFO and a mode-000 file
#: (`gen_symbols`);
#: ``project_file_parse_posix`` parses a real tree with a FIFO, links, a
#: loop and a mode-000 file, worded by the C library (`gen_project_file`).
#: ``store_event_reads_naive`` reads a naive instant under a ``TZ`` the
#: generator sets with `time.tzset`, which Windows lacks
#: (`gen_store_event_reads`).
POSIX_ONLY = frozenset(
    {
        "store_event_reads_naive",
        # `os.fsencode` encodes a surrogate with `surrogatepass` on Windows.
        "response_write_posix",
        "attestation_paths_posix",
        "project_walk",
        # The gates resolve paths through `posixpath`; `ntpath` asks Windows.
        "handlers_verify_gates",
        "handlers_verify_gates_python",
        "symbols_platform",
        "checkout_token_read",
        "checkout_token_read_posix",
        "checkout_token_write",
        "checkout_token_write_posix",
        "verify_candidates_scan",
        "verify_candidates_scan_unicode",
        "verify_anchoring_attest_posix",
        "verify_anchoring_detect",
        # The anchors' compare form is the host's `Path` (`gen_verify_count`).
        "verify_count",
        "checkout_token_remint",
        "checkout_token_remint_posix",
        "project_file_parse_posix",
        "store_restore_lock",
        "store_restore_lock_hold",
        "store_restore_lock_python",
        "store_restore_lock_hold_python",
        # Locks, a FIFO, links and an unreadable record (`gen_store_refusal`).
        "store_refusal_posix",
        "store_schema_posix",
        "store_upgrade_posix",
        "settings_paths",
        "settings_paths_python",
        "settings_create_posix",
        "store_open",
        "store_write",
        "store_tx",
        "store_records",
        "store_records_b",
        "store_records_c",
        "store_records_d",
        "store_records_e",
        "store_returned_posix",
        "store_record_event_posix",
        "signed_key_sort_posix_python",
        "store_membership",
        "store_rotation",
        "store_rotation_python",
        "store_rows_written",
        "githead_posix",
        "gitobjects_files_posix",
        "githead_discovery",
        "gitobjects_reader_posix",
        "githead_signature",
        "origin_history_located",
        "origin_history_located_modes",
        "location_probe",
        "location_probe_python",
        "location_probe_errno",
        "machine_open",
        "machine_open_platform",
        "gitconfig_file",
        "location_stamps",
        "location_stamps_python",
        "log_keyfiles_posix",
        "log_keyfiles_posix_python",
        "log_keyfiles_darwin",
        "gitconfig_tree",
        "gitconfig_legacy",
        "gitconfig_legacy_python",
        "location_remotes",
        "location_spellings",
        "location_stamped",
        "location_git_readers_posix",
        "location_view_posix",
        "location_discovery",
        "location_leaves_posix",
        "location_cache_posix",
        "origin_gitfiles_machine",
        "origin_gitfiles_machine_python",
        "origin_gitfiles_darwin",
        "origin_gitfiles_facts_posix",
        "origin_gitfiles_root_posix",
        "origin_gitfiles_attrs_posix",
        "origin_gitfiles_walk_posix",
        "origin_cache_posix",
        "gitsystem_record",
        "gitsystem_record_python",
        "gitsystem_probe",
        "gitsystem_probe_python",
        "gitsystem_ask",
        "gitsystem_ask_unicode",
        "gitsystem_ask_python",
        "gitsystem_sources",
        "gitsystem_sources_python",
        "gitfile_leaves_posix",
        "store_provenance",
        "verify_anchoring_skip",
        "fs_realpath",
        "fs_realpath_python",
        "store_bulk",
        "fs_stat",
        "errno_class",
        "fs_access",
        "fs_realpath_strict",
        "fs_realpath_strict_python",
        "fs_samestat",
        "gitdir_read",
        "gitdir_read_python",
        "gitfile_identity",
        "gitfile_identity_python",
        "gitfile_trust",
        "gitfile_trust_python",
        "gitfile_discovery",
        "gitfile_discovery_python",
        "gitfile_follow",
        "gitfile_follow_python",
    }
)


def platform_key() -> str:
    """``sys.platform``-``machine``; ``BM_PLATFORM_KEY`` overrides it, to
    simulate another platform (the native tests read it too)."""
    override = os.environ.get("BM_PLATFORM_KEY")
    if override:
        return override
    return f"{sys.platform}-{platform.machine().lower()}"


def _libc() -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    name, version = platform.libc_ver()
    return f"{name} {version}".strip() or None


def drift(name: str) -> list[str]:
    """The pins generator ``name``'s committed files were made under that
    this interpreter and platform do not share."""
    out = []
    if name in UNICODE_GATED and unicodedata.unidata_version != UNIDATA_VERSION:
        out.append(f"Unicode {unicodedata.unidata_version} (files: {UNIDATA_VERSION})")
    if name in SQLITE_GATED and sqlite3.sqlite_version != SQLITE_VERSION:
        out.append(f"SQLite {sqlite3.sqlite_version} (files: {SQLITE_VERSION})")
    if name in PYTHON_GATED and python_version() != PYTHON_VERSION:
        out.append(f"Python {python_version()} (files: {PYTHON_VERSION})")
    if name in ZLIB_GATED and zlib_version() != ZLIB_VERSION:
        out.append(f"zlib {zlib_version()} (files: {ZLIB_VERSION})")
    if name in PLATFORM_GATED and platform_key() != PLATFORM:
        out.append(f"platform {platform_key()} (files: {PLATFORM})")
    if name in PYDANTIC_GATED and pydantic_core_version() != PYDANTIC_CORE_VERSION:
        out.append(
            f"pydantic-core {pydantic_core_version()} (files: {PYDANTIC_CORE_VERSION})"
        )
    return out


class LeftStateError(RuntimeError):
    """A generator left process-wide state of the reference other than it
    found it, which a later generator in the same process would read."""


def _process_state() -> dict[str, object]:
    """The process-wide state of the reference a generator may set and a
    later one reads: every ContextVar of the package (the published caller
    first among them), the working directory and the environment.

    Compared by identity for the ContextVars: a generator that restores
    what it found (``ContextVar.reset``) gives back the same object. The
    memo caches ``bettermemory._caches`` registers are left out: each is
    exact by contract, so a warm one changes no answer. Module globals a
    generator replaces without ``mock.patch`` are left out too: no
    registry names them, and every generator here patches through
    ``mock.patch``, which restores on exit."""
    from bettermemory import config, events, git_read_budget, identity, store_index

    state: dict[str, object] = {
        "identity._CURRENT": identity._CURRENT.get(),
        "config._TRIAL": config._TRIAL.get(),
        "events._SUPPRESSED": events._SUPPRESSED.get(),
        "events._DEADLINE": events._DEADLINE.get(),
        "git_read_budget._OWNER": git_read_budget._OWNER.get(),
        "git_read_budget._ACTIVE": git_read_budget._ACTIVE.get(),
        "git_read_budget._ROOT": git_read_budget._ROOT.get(),
        "store_index._DRIFT_SUBJECT": store_index._DRIFT_SUBJECT.get(),
    }
    return state


def _generator_context() -> Context:
    """Isolate known mutable package values and keep shared budget aliases."""
    from bettermemory import config, git_read_budget, identity

    context = copy_context()
    if isinstance(caller := identity.current(), identity.Caller):
        context.run(identity._CURRENT.set, caller.model_copy(deep=True))
    if isinstance(trial := config._TRIAL.get(), list):
        context.run(config._TRIAL.set, trial.copy())

    # ReadBudget and its progress may be shared by two bindings. Copy their
    # graph with one memo, substituting a fresh lock for each progress lock.
    memo: dict[int, object] = {}
    for variable in (git_read_budget._OWNER, git_read_budget._ACTIVE):
        budget = variable.get()
        if isinstance(budget, git_read_budget.ReadBudget):
            if budget.progress is not None:
                memo.setdefault(id(budget.progress.lock), RLock())
            context.run(variable.set, deepcopy(budget, memo))
    return context


def _mutable_state() -> dict[str, object]:
    """Snapshot fields whose values may change without rebinding a ContextVar."""
    from bettermemory import config, git_read_budget, identity

    values: dict[str, object] = {}
    if isinstance(caller := identity.current(), identity.Caller):
        values["identity._CURRENT"] = caller.model_dump()
    if isinstance(trial := config._TRIAL.get(), list):
        values["config._TRIAL"] = tuple(trial)
    for name, variable in (
        ("git_read_budget._OWNER", git_read_budget._OWNER),
        ("git_read_budget._ACTIVE", git_read_budget._ACTIVE),
    ):
        if isinstance(budget := variable.get(), git_read_budget.ReadBudget):
            progress = (
                (budget.progress.resume,) if budget.progress is not None else None
            )
            values[name] = replace(budget, progress=None), progress
    return values


def _same_directory(identity: os.stat_result) -> bool:
    try:
        os.getcwd()  # Unlinked directories may still have a matching inode.
        return os.path.samestat(os.stat("."), identity)
    except OSError:
        return False


@contextmanager
def _cwd_handle() -> Iterator[int | None]:
    fd = None
    if os.chdir in os.supports_fd:
        search = getattr(os, "O_SEARCH", getattr(os, "O_PATH", os.O_RDONLY))
        for flags in dict.fromkeys((search, os.O_RDONLY)):
            try:
                fd = os.open(".", flags)
                break
            except OSError:
                pass
    try:
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def _restore_environment(
    cwd: str, identity: os.stat_result, fd: int | None, environ: dict[str, str]
) -> list[str]:
    """Describe and undo changes before the next generator runs."""
    left = []
    try:
        current_dir: str | None = os.getcwd()
        moved = current_dir != cwd or not _same_directory(identity)
    except OSError:
        current_dir, moved = None, True
    if moved:
        left.append(f"the working directory ({cwd!r} to {current_dir!r})")
        try:
            # A generator may have renamed the directory it started in.
            os.chdir(fd if fd is not None else cwd)
            if not _same_directory(identity):
                left.append("could not restore the original working directory identity")
        except OSError as exc:
            left.append(f"could not restore the working directory: {exc}")

    changed = sorted(
        key
        for key in environ.keys() | os.environ.keys()
        if environ.get(key) != os.environ.get(key)
    )
    for key in changed:
        if key in environ:
            os.environ[key] = environ[key]
        else:
            del os.environ[key]
    if changed:
        left.append("the environment (" + ", ".join(changed) + ")")
    return left


def run_generator(
    name: str, generate: Callable[[], dict[str, bytes]] | None = None
) -> dict[str, bytes]:
    """Run a generator with the caller's context, preserving it for the next run.

    A leak on success raises LeftStateError. A generator that raises retains
    its original error and gets a diagnostic note if it also leaked state.
    """
    if generate is None:
        generate = GENERATORS[name]
    context = _generator_context()
    before = context.run(_process_state)
    mutable = context.run(_mutable_state)
    cwd, directory, environ = os.getcwd(), os.stat("."), dict(os.environ)
    with _cwd_handle() as fd:
        failure: BaseException | None = None
        try:
            return context.run(generate)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            after = context.run(_process_state)
            actual = context.run(_mutable_state) if mutable else {}
            left = [
                key
                for key in before
                if after[key] is not before[key]
                or (key in mutable and mutable[key] != actual.get(key))
            ]
            left.extend(_restore_environment(cwd, directory, fd, environ))
            if left:
                message = (
                    f"generator {name} left process-wide state set: {'; '.join(left)}"
                )
                if failure is None:
                    raise LeftStateError(message)
                failure.add_note(message)


def as_found(out: Path) -> int:
    """Every generator under whatever is present, into ``out``, with
    ``platform.json``. Exit 0 when every generator ran, 1 when one raised
    (the files of the others are written all the same). A lost working
    directory stops the run before another fixture can use the wrong tree."""
    from ._diff import _differing

    directory = os.stat(".")
    out.mkdir(parents=True, exist_ok=True)
    generators: dict[str, dict[str, object]] = {}
    files: dict[str, object] = {}
    failed = 0
    for name, generate in GENERATORS.items():
        entry: dict[str, object] = {"gated_by": drift(name)}
        if name in POSIX_ONLY and sys.platform == "win32":
            entry["not_here"] = "POSIX only: no case on Windows"
            generators[name] = entry
            print(f"{name}: not generated here (POSIX only)")
            continue
        started = time.monotonic()
        try:
            produced = run_generator(name, generate)
        except Exception as exc:
            # A relative input or output path would now name the wrong fixture.
            if not _same_directory(directory):
                raise
            failed += 1
            entry["raised"] = f"{type(exc).__name__}: {exc}"
            entry["traceback"] = traceback.format_exc().splitlines()[-12:]
            generators[name] = entry
            print(f"{name}: raised {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        entry["files"] = sorted(produced)
        entry["seconds"] = round(time.monotonic() - started, 1)
        generators[name] = entry
        # Flushed: a runner's log then shows where the minutes go.
        print(f"{name}: {entry['seconds']} s", flush=True)
        for filename, data in sorted(produced.items()):
            (out / filename).write_bytes(data)
            committed_path = GOLDEN / filename
            committed = committed_path.read_bytes() if committed_path.exists() else None
            info: dict[str, object] = {
                "generator": name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "identical": data == committed,
            }
            if committed is None:
                info["committed"] = None
            elif data != committed:
                if filename.endswith(".sqlite"):
                    # A store file as this SQLite wrote it: its bytes are
                    # not lines; store_real.json records its answers.
                    info["differs"] = (
                        f"binary, {len(data)} bytes here, {len(committed)} committed"
                    )
                else:
                    ours, theirs = data.splitlines(), committed.splitlines()
                    info["lines_differing"] = sum(
                        1
                        for i in range(max(len(ours), len(theirs)))
                        if (ours[i] if i < len(ours) else None)
                        != (theirs[i] if i < len(theirs) else None)
                    )
                    info["differs"] = _differing(data, committed)
            files[filename] = info
            print(
                f"{filename}: {len(data)} bytes, "
                f"{'identical' if info['identical'] else 'differs'}"
            )
    manifest = {
        "platform": platform_key(),
        "machine": platform.machine(),
        "system": platform.platform(),
        "libc": _libc(),
        "python": python_version(),
        "python_build": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "unicode": unicodedata.unidata_version,
        "zlib": zlib_version(),
        "pydantic_core": pydantic_core_version(),
        "committed": {
            "platform": PLATFORM,
            "python": PYTHON_VERSION,
            "sqlite": SQLITE_VERSION,
            "unicode": UNIDATA_VERSION,
            "zlib": ZLIB_VERSION,
            "pydantic_core": PYDANTIC_CORE_VERSION,
        },
        "generators": generators,
        "raised": sorted(n for n, e in generators.items() if "raised" in e),
        "not_here": sorted(n for n, e in generators.items() if "not_here" in e),
        "files": files,
    }
    (out / "platform.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"platform.json: {platform_key()}, Python {python_version()}, "
        f"SQLite {sqlite3.sqlite_version}, Unicode {unicodedata.unidata_version}; "
        f"{failed} generator(s) raised"
    )
    return 1 if failed else 0


def python_version() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.contract.generate")
    parser.add_argument("--out", type=Path, help="write into this directory")
    parser.add_argument(
        "--as-found",
        action="store_true",
        help="run under whatever is present, write platform.json (needs --out)",
    )
    args = parser.parse_args(argv)
    if args.as_found:
        if args.out is None:
            parser.error("--as-found needs --out DIR")
        if args.out.resolve() == GOLDEN.resolve():
            parser.error("--as-found never writes the committed directory")
        return as_found(args.out)
    if args.out is not None:
        parser.error(
            "--out without --as-found: the pinned run writes the committed set"
        )
    if unicodedata.unidata_version != UNIDATA_VERSION:
        print(
            f"refusing: Unicode {unicodedata.unidata_version}, golden files are "
            f"{UNIDATA_VERSION}",
            file=sys.stderr,
        )
        return 1
    if sqlite3.sqlite_version != SQLITE_VERSION:
        print(
            f"refusing: SQLite {sqlite3.sqlite_version}, golden files are "
            f"{SQLITE_VERSION}",
            file=sys.stderr,
        )
        return 1
    if python_version() != PYTHON_VERSION:
        print(
            f"refusing: Python {python_version()}, golden files are {PYTHON_VERSION}",
            file=sys.stderr,
        )
        return 1
    if zlib_version() != ZLIB_VERSION:
        print(
            f"refusing: zlib {zlib_version()}, golden files are {ZLIB_VERSION}",
            file=sys.stderr,
        )
        return 1
    if pydantic_core_version() != PYDANTIC_CORE_VERSION:
        print(
            f"refusing: pydantic-core {pydantic_core_version()}, golden files are "
            f"{PYDANTIC_CORE_VERSION}",
            file=sys.stderr,
        )
        return 1
    GOLDEN.mkdir(exist_ok=True)
    for generator in GENERATORS:
        for name, data in run_generator(generator).items():
            (GOLDEN / name).write_bytes(data)
            print(f"{name}: {len(data)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
