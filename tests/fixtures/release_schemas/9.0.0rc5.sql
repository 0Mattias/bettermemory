-- The store schema 9.0.0rc5 to 9.0.0rc6 created, frozen from the release
-- tags' schema texts; the full-text table's shadow tables are
-- FTS5's own, made by SQLite when the virtual table is (ENG-107).
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE memories (
    rowid INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    last_verified_at TEXT,
    confidence TEXT NOT NULL,
    category TEXT,
    body TEXT NOT NULL,
    body_fts TEXT NOT NULL DEFAULT '',
    scopes_text TEXT NOT NULL,
    scopes_fts TEXT NOT NULL DEFAULT '',
    scopes_json TEXT NOT NULL,
    filename TEXT,
    origin_repo TEXT,
    origin_worktree TEXT,
    provenance TEXT NOT NULL,
    verified_head TEXT,
    actor_client TEXT,
    actor_model TEXT,
    source TEXT NOT NULL,
    origin_json TEXT,
    actor_json TEXT,
    verified_paths_json TEXT NOT NULL DEFAULT '[]',
    verified_commits_json TEXT NOT NULL DEFAULT '[]',
    verified_versions_json TEXT NOT NULL DEFAULT '[]',
    verified_absent_paths_json TEXT NOT NULL DEFAULT '[]',
    claims_json TEXT NOT NULL DEFAULT '[]',
    links_json TEXT NOT NULL DEFAULT '[]',
    corroborations INTEGER NOT NULL DEFAULT 0,
    last_corroborated TEXT,
    log_mac TEXT
);
CREATE VIRTUAL TABLE memories_fts USING fts5(
    body_fts, scopes_fts,
    content='memories', content_rowid='rowid',
    tokenize='unicode61'
);
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, body_fts, scopes_fts)
    VALUES (new.rowid, new.body_fts, new.scopes_fts);
END;
CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, body_fts, scopes_fts)
    VALUES ('delete', old.rowid, old.body_fts, old.scopes_fts);
END;
CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, body_fts, scopes_fts)
    VALUES ('delete', old.rowid, old.body_fts, old.scopes_fts);
    INSERT INTO memories_fts(rowid, body_fts, scopes_fts)
    VALUES (new.rowid, new.body_fts, new.scopes_fts);
END;
CREATE INDEX memories_by_updated ON memories(updated DESC);
CREATE TABLE memory_links (
    source_id TEXT NOT NULL,
    type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (source_id, type, target_id, note)
);
CREATE INDEX memory_links_by_target ON memory_links(target_id);
CREATE TRIGGER memory_links_cleanup AFTER DELETE ON memories BEGIN
    DELETE FROM memory_links
    WHERE source_id = old.id OR target_id = old.id;
END;
CREATE TABLE tombstones (
    id TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    last_verified_at TEXT,
    confidence TEXT NOT NULL,
    category TEXT,
    body TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    filename TEXT,
    origin_repo TEXT,
    origin_worktree TEXT,
    provenance TEXT NOT NULL,
    verified_head TEXT,
    actor_client TEXT,
    actor_model TEXT,
    source TEXT NOT NULL,
    origin_json TEXT,
    actor_json TEXT,
    verified_paths_json TEXT NOT NULL DEFAULT '[]',
    verified_commits_json TEXT NOT NULL DEFAULT '[]',
    verified_versions_json TEXT NOT NULL DEFAULT '[]',
    verified_absent_paths_json TEXT NOT NULL DEFAULT '[]',
    claims_json TEXT NOT NULL DEFAULT '[]',
    links_json TEXT NOT NULL DEFAULT '[]',
    corroborations INTEGER NOT NULL DEFAULT 0,
    last_corroborated TEXT,
    removed TEXT NOT NULL,
    removed_reason TEXT NOT NULL,
    removed_session TEXT,
    log_mac TEXT
);
CREATE INDEX tombstones_by_removed ON tombstones(removed);
CREATE TABLE episodes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created TEXT NOT NULL,
    body TEXT NOT NULL,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    takeaway TEXT,
    origin_json TEXT,
    is_floor INTEGER NOT NULL DEFAULT 0,
    swarm_id TEXT,
    log_mac TEXT
);
CREATE INDEX episodes_by_session ON episodes(session_id, created);
CREATE TABLE log (
    seq INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    session TEXT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_mac TEXT NOT NULL,
    mac TEXT NOT NULL
);
CREATE INDEX log_by_kind ON log(kind, seq);
CREATE INDEX log_by_ts ON log(ts);
CREATE INDEX log_by_mac ON log(mac);
CREATE TABLE verifications (
    memory_id TEXT NOT NULL,
    machine_id TEXT,
    verified_at TEXT NOT NULL,
    verified_head TEXT,
    verified_paths_json TEXT NOT NULL DEFAULT '[]',
    verified_commits_json TEXT NOT NULL DEFAULT '[]',
    verified_versions_json TEXT NOT NULL DEFAULT '[]',
    verified_absent_paths_json TEXT NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX verifications_by_memory_machine
    ON verifications(memory_id, COALESCE(machine_id, ''));
CREATE TABLE conflicts (
    id TEXT PRIMARY KEY,
    a_id TEXT NOT NULL,
    b_id TEXT NOT NULL,
    summary_a TEXT NOT NULL,
    summary_b TEXT NOT NULL,
    similarity REAL NOT NULL,
    method TEXT NOT NULL,
    detector TEXT NOT NULL,
    created TEXT NOT NULL,
    status TEXT NOT NULL,
    verdict_ts TEXT,
    note TEXT,
    verdict_hash_a TEXT,
    verdict_hash_b TEXT
);
CREATE TABLE imports (
    source TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    memory_id TEXT
);
CREATE TABLE quarantine (
    name TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    detail TEXT,
    remote TEXT,
    at TEXT NOT NULL,
    size INTEGER,
    sha256 TEXT
);
CREATE TABLE machines (
    id TEXT PRIMARY KEY,
    facts_json TEXT NOT NULL DEFAULT '{}',
    predecessor TEXT,
    minted TEXT NOT NULL
);
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    merged_into TEXT,
    provisional TEXT,
    parent TEXT,
    tenant TEXT,
    folder INTEGER NOT NULL DEFAULT 0,
    boundary INTEGER NOT NULL DEFAULT 0,
    minted TEXT NOT NULL
);
CREATE INDEX projects_by_merged_into ON projects(merged_into);
CREATE TABLE project_witnesses (
    project TEXT NOT NULL,
    commit_id TEXT NOT NULL,
    arrival INTEGER NOT NULL,
    source TEXT NOT NULL,
    fingerprint TEXT,
    PRIMARY KEY (project, commit_id)
);
CREATE INDEX project_witnesses_by_commit ON project_witnesses(commit_id);
CREATE INDEX project_witnesses_by_fingerprint
    ON project_witnesses(fingerprint);
CREATE TABLE project_blobs (
    project TEXT NOT NULL,
    blob TEXT NOT NULL,
    witness TEXT NOT NULL,
    PRIMARY KEY (project, blob)
);
CREATE INDEX project_blobs_by_witness ON project_blobs(project, witness);
CREATE TABLE project_remotes (
    project TEXT NOT NULL,
    remote_key TEXT NOT NULL,
    PRIMARY KEY (project, remote_key)
);
CREATE INDEX project_remotes_by_key ON project_remotes(remote_key);
CREATE TABLE project_files (
    file_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    evidence TEXT NOT NULL
);
CREATE INDEX project_files_by_project ON project_files(project);
CREATE TABLE declared_remotes (
    remote_key TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    principal TEXT NOT NULL
);
CREATE TABLE checkouts (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    machine TEXT NOT NULL,
    kind TEXT NOT NULL,
    object_device TEXT,
    object_inode TEXT,
    object_birth TEXT,
    common_device TEXT,
    common_inode TEXT,
    common_birth TEXT,
    git_dir TEXT,
    token TEXT,
    token_status TEXT NOT NULL,
    stamps_json TEXT NOT NULL DEFAULT '[]',
    last_head TEXT,
    branch TEXT,
    remotes_json TEXT NOT NULL DEFAULT '[]',
    manifest_json TEXT,
    legacy INTEGER NOT NULL DEFAULT 0,
    retired INTEGER NOT NULL DEFAULT 0,
    registered TEXT NOT NULL,
    unresolved_json TEXT
);
CREATE INDEX checkouts_by_object
    ON checkouts(machine, object_device, object_inode);
CREATE INDEX checkouts_by_common
    ON checkouts(machine, common_device, common_inode);
CREATE INDEX checkouts_by_token ON checkouts(token);
CREATE INDEX checkouts_by_project ON checkouts(project);
CREATE TABLE checkout_views (
    checkout TEXT NOT NULL,
    machine TEXT NOT NULL,
    path TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    seen_order INTEGER NOT NULL,
    PRIMARY KEY (checkout, machine, path)
);
CREATE INDEX checkout_views_by_path ON checkout_views(machine, path);
CREATE TABLE homes (
    record_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    project TEXT NOT NULL,
    checkout TEXT,
    rule TEXT NOT NULL,
    decided TEXT NOT NULL
);
CREATE INDEX homes_by_project ON homes(project);
CREATE INDEX homes_by_checkout ON homes(checkout);
CREATE TABLE proposals (
    id TEXT PRIMARY KEY,
    checkout TEXT,
    kind TEXT NOT NULL,
    candidates_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL,
    line TEXT NOT NULL,
    state TEXT NOT NULL,
    answer TEXT,
    answered_by TEXT,
    signature_json TEXT,
    opened TEXT NOT NULL
);
CREATE INDEX proposals_by_checkout ON proposals(checkout, state);
