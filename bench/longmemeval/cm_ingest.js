/**
 * claude-mem ingest for the LongMemEval harness. Runs under BUN
 * (SessionStore requires `bun:sqlite`).
 *
 * Reads a JSON job on stdin:
 *   { dataDir, project, sessions: [ { session_id, date, rounds: [text] } ] }
 *
 * Writes one observation per conversational round, with
 * `memory_session_id` set to the LongMemEval session id — claude-mem has a
 * native session column, so the attribution rule the pre-registration
 * fixes lands on their own schema rather than being imposed from outside.
 *
 * Emits JSON on stdout:
 *   { sessions_written, rounds_offered, items_written, shortfall }
 *
 * WHY THE TITLES AND EPOCHS ARE FORCED UNIQUE. `importObservation`
 * dedups on (memory_session_id, title, created_at_epoch) and silently
 * returns the existing row otherwise. bettermemory's `Store.write` dedups
 * on nothing, so without this the two arms would receive different
 * corpora and the comparison would be measuring an import quirk. Each
 * round therefore gets its own title and its own epoch.
 *
 * WHAT IS DELIBERATELY LEFT EMPTY, and it cuts against claude-mem.
 * `observations_fts` spans title/subtitle/narrative/text/facts/concepts.
 * Their real pipeline fills all six via an LLM extraction pass. This
 * harness fills `text` (and a synthetic ordering title) and leaves the
 * rest null, because enriching would require an API key at write time and
 * would mean authoring a competitor's extraction ourselves. See
 * PREREGISTRATION.md addendum 2 — this may understate them and must be
 * published beside any number.
 */

const SS_PATH = new URL('./vendor/package/plugin/sqlite/SessionStore.js', import.meta.url).pathname;

function readStdin() {
  return new Promise((resolve, reject) => {
    let s = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (d) => (s += d));
    process.stdin.on('end', () => resolve(s));
    process.stdin.on('error', reject);
  });
}

const job = JSON.parse(await readStdin());
const { dataDir, project, sessions } = job;

// Must be set before SessionStore is required: the module resolves its
// data directory once, at load time.
process.env.CLAUDE_MEM_DATA_DIR = dataDir;
process.env.CLAUDE_MEM_LOG_LEVEL = 'error';

const { SessionStore } = await import(SS_PATH);
const store = new SessionStore();

let roundsOffered = 0;
let itemsWritten = 0;
let sessionsWritten = 0;

for (let si = 0; si < sessions.length; si++) {
  const s = sessions[si];
  // Session dates in the corpus are human strings; fall back to a
  // deterministic synthetic clock so ordering stays stable and distinct
  // when a date is missing or unparseable.
  const parsed = s.date ? Date.parse(s.date) : NaN;
  const baseEpoch = Number.isNaN(parsed) ? 1_600_000_000_000 + si * 86_400_000 : parsed;

  store.importSdkSession({
    content_session_id: `content-${s.session_id}`,
    memory_session_id: s.session_id,
    project,
    platform_source: 'claude-code',
    user_prompt: null,
    started_at: new Date(baseEpoch).toISOString(),
    started_at_epoch: baseEpoch,
    completed_at: null,
    completed_at_epoch: null,
    status: 'completed',
  });
  sessionsWritten++;

  for (let ri = 0; ri < s.rounds.length; ri++) {
    roundsOffered++;
    // Unique per round: defeats the (session, title, epoch) dedup that
    // would otherwise collapse repeated conversational filler.
    const epoch = baseEpoch + ri * 1000;
    const res = store.importObservation({
      memory_session_id: s.session_id,
      project,
      text: s.rounds[ri],
      type: 'observation',
      title: `r${si}-${ri}`,
      subtitle: null,
      facts: null,
      narrative: null,
      concepts: null,
      files_read: null,
      files_modified: null,
      prompt_number: ri,
      discovery_tokens: 0,
      agent_type: null,
      agent_id: null,
      created_at: new Date(epoch).toISOString(),
      created_at_epoch: epoch,
    });
    if (res && res.imported) itemsWritten++;
  }
}

store.rebuildObservationsFTSIndex();
store.close();

process.stdout.write(
  JSON.stringify({
    sessions_written: sessionsWritten,
    rounds_offered: roundsOffered,
    items_written: itemsWritten,
    shortfall: roundsOffered ? 1 - itemsWritten / roundsOffered : 0,
  }) + '\n'
);
