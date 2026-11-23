//! Key and reserved-kind checks precede coercion, even for large raw inputs.
mod timing;

use bettermemory_core::{
    event_hook::HookScope,
    event_recorder::Recorder,
    identity::Published,
    pyvalue::PyValue,
    store::{record_event::Logged, recorder::StoreEventSink},
    unicode::cps,
};

#[test]
fn large_non_scalar_inputs_never_trigger_work_on_a_keyless_connection() {
    let mut log = Vec::new();
    let refusal = vec![0xdcff, 0x1f980];
    let sink = StoreEventSink {
        target: Err(&refusal),
        log: &mut log,
        spans: &mut |_| panic!("keyless redaction"),
    };
    let mut recorder = Recorder {
        store: sink,
        session_id: vec![],
        enabled: true,
        worktree_root: None,
        attribution: vec![],
        triggered_from: vec![],
        secret_spans: Box::new(|_| panic!("unexpected state redaction")),
        declaration: Box::new(|_| Ok(None)),
        capture_worktree: Box::new(|| unreachable!()),
        warn: Box::new(|_| unreachable!()),
    };
    let mut nested = PyValue::int(2);
    for _ in 0..15_000 {
        nested = PyValue::List(vec![nested]);
    }
    let fields = vec![
        (cps("query"), PyValue::Str(vec![0xdc00; 180_000])),
        (cps("actor"), nested),
        (cps("deadline"), PyValue::Int("9".repeat(8000))),
        (cps("attempts"), PyValue::str("invalid")),
    ];
    let kind = vec![0xdcff; 230_000];
    recorder
        .record_typed(
            &HookScope::new(),
            &Published::default(),
            &kind,
            fields,
            &mut |_| panic!("key refusal is a store warning"),
        )
        .unwrap();
    let mut warnings = 0;
    recorder
        .record_typed(
            &HookScope::new(),
            &Published::default(),
            &cps("memory_put"),
            vec![],
            &mut |w| {
                warnings += 1;
                assert_eq!(w.error.exception.class, "ValueError");
                Ok(())
            },
        )
        .unwrap();
    assert_eq!(warnings, 1);
    assert_eq!(log, vec![Logged::NotRecorded(refusal)]);
}

#[test]
fn converting_many_distinct_actor_pairs_has_linear_growth() {
    use bettermemory_core::event_recorder::ActorKeyObjects;
    use bettermemory_core::store::recorder::actor_record;
    let pairs = |size| {
        PyValue::List(
            (0..size)
                .map(|i| {
                    PyValue::Tuple(vec![
                        PyValue::str(&format!("client-{i}")),
                        PyValue::int(i as i128),
                    ])
                })
                .collect(),
        )
    };
    let small = pairs(1_000);
    let large = pairs(10_000);
    let keys = ActorKeyObjects::default();
    timing::grows_within(
        "actor pair conversion",
        timing::TENFOLD,
        &mut || timing::once(|| actor_record(&small, &keys).unwrap()),
        &mut || timing::once(|| actor_record(&large, &keys).unwrap()),
    );
}
