//! Hostile patterns and texts for the matcher: no panic, every match ends
//! within its budget (spent where it would backtrack without end), and
//! the work is measured by the step counter itself, not by a clock.

use bettermemory_core::gitconfig::wildmatch::{
    any_match, wildmatch, wildmatch_scanned, Spent, Steps, WM_DEPTH, WM_STEPS,
};

const TEN_MB: usize = 10 << 20;

fn run(pattern: &[u8], text: &[u8], icase: bool) -> (Result<bool, Spent>, i64) {
    let mut steps = Steps::default();
    let found = wildmatch(pattern, text, icase, &mut steps);
    (found, WM_STEPS - steps.left)
}

#[test]
fn backtracking_shapes_are_spent_within_the_budget() {
    // `**/` repeated against a deep path: each `**` tries every depth.
    let pattern = [b"**/".repeat(24), b"x".to_vec()].concat();
    for icase in [false, true] {
        let (found, taken) = run(&pattern, &b"a/".repeat(60), icase);
        assert_eq!(found, Err(Spent::Steps));
        assert_eq!(taken, WM_STEPS + 1);
    }
    // `*a*a...*b` against a run of `a`: git's matcher aborts at the first
    // star that cannot find its byte, so this is linear (the reference
    // takes 601 steps here and 91 and 181 for the shorter shapes).
    let pattern = [b"*a".repeat(30), b"b".to_vec()].concat();
    assert_eq!(run(&pattern, &b"a".repeat(200), false), (Ok(false), 601));
    let pattern = [b"*a".repeat(10), b"b".to_vec()].concat();
    assert_eq!(run(&pattern, &b"a".repeat(30), false), (Ok(false), 91));
    assert_eq!(run(&pattern, &b"a".repeat(60), false), (Ok(false), 181));
}

#[test]
fn stars_past_the_depth_are_spent() {
    let n = WM_DEPTH + 2;
    let (found, _) = run(&b"*a".repeat(n), &b"a".repeat(n), false);
    assert_eq!(found, Err(Spent::Depth));
    let (found, _) = run(&b"*a".repeat(10_000), &b"a".repeat(10_000), false);
    assert_eq!(found, Err(Spent::Depth));
}

#[test]
fn ten_megabyte_inputs_end() {
    let big = b"a".repeat(TEN_MB);
    // A literal as long as the budget, then one byte past it.
    let n = WM_STEPS as usize;
    assert_eq!(run(&big[..n], &big[..n], false), (Ok(true), WM_STEPS));
    assert_eq!(
        run(&big[..n + 1], &big[..n + 1], false),
        (Err(Spent::Steps), WM_STEPS + 1)
    );
    // A pattern of 10 MB of stars is one star.
    let (found, taken) = run(&b"*".repeat(TEN_MB), &big, false);
    assert_eq!((found, taken), (Ok(true), 1));
    // An unterminated bracket of 10 MB is spent byte by byte.
    let bracket = [b"[".to_vec(), b"a".repeat(TEN_MB)].concat();
    assert_eq!(run(&bracket, b"a", false).0, Err(Spent::Steps));
    // A star before a byte the text lacks scans once and aborts.
    let (found, taken) = run(b"*b", &big, false);
    assert_eq!((found, taken), (Ok(false), 2));
    let (found, _) = run(b"*a*a*a*b", &big, true);
    assert_eq!(found, Ok(false));
    let nul = vec![0u8; TEN_MB];
    assert_eq!(run(b"\\", &nul[..1], false), (Ok(true), 1));
    assert_eq!(run(&nul[..1000], &nul[..1000], false), (Ok(true), 1000));
}

#[test]
fn many_texts_share_one_budget() {
    let pattern = [b"**/".repeat(24), b"x".to_vec()].concat();
    let text = b"a/".repeat(60);
    let texts = vec![text.as_slice(); 10_000];
    let mut steps = Steps::default();
    assert_eq!(any_match(&pattern, texts, &mut steps, false), None);
    // The budget is spent by the first text, and each later one is
    // refused at its first step (the reference: -100 after 100 texts).
    assert_eq!(steps.left, -10_000);
}

#[test]
fn steps_scale_with_the_input_not_beyond() {
    // The counter is the measure of the work: a literal takes one step a
    // byte, and `*b` and `?*b` one step a byte of the text (the reference:
    // 2001, 2002 and 20002 steps for 1,000 and 10,000 pairs).
    for n in [1_000usize, 10_000] {
        let text = b"ab".repeat(n);
        assert_eq!(run(&text, &text, false), (Ok(true), 2 * n as i64));
        assert_eq!(run(b"*b", &text, false), (Ok(true), 2 * n as i64 + 1));
        assert_eq!(run(b"?*b", &text, false), (Ok(true), 2 * n as i64 + 2));
    }
    let (_, one) = run(b"?*b", &b"ab".repeat(1_000), false);
    let (_, ten) = run(b"?*b", &b"ab".repeat(10_000), false);
    assert!(
        ten <= 10 * one,
        "{one} steps, then {ten} for ten times the text"
    );
    // Past the budget the work stops, whatever the text's length.
    assert_eq!(
        run(b"?*b", &b"ab".repeat(1 << 20), false),
        (Err(Spent::Steps), WM_STEPS + 1)
    );
}

fn scanned(pattern: &[u8], text: &[u8]) -> (Result<bool, Spent>, i64, u64) {
    let mut steps = Steps::default();
    let (found, read) = wildmatch_scanned(pattern, text, false, &mut steps);
    (found, WM_STEPS - steps.left, read)
}

#[test]
fn searches_for_a_slash_read_the_text_about_once() {
    // The reference reads 8,749,725,002 bytes here in 43,749 searches for
    // a `/`, each over the rest of the text; the index answers each search
    // after the second in two words.
    let pattern = b"[!/]***a?**/**/*a";
    let text = vec![b'a'; 200_000];
    assert_eq!(
        scanned(pattern, &text),
        (Err(Spent::Steps), WM_STEPS + 1, 249_997)
    );
    // 3,199,980,000 bytes in the reference.
    let text = [vec![b'a'; 100_000], vec![b'/'; 100_000]].concat();
    assert_eq!(
        scanned(b"*a\\a*/b*", &text),
        (Err(Spent::Steps), WM_STEPS + 1, 139_999)
    );
    // The worst found: the literal scans of six stars, which the reference
    // reads byte by byte as well, six times the text, linear in it.
    let pattern = b"**/**/**/**/****/*/aa****a*/";
    for n in [2_000usize, 200_000] {
        assert_eq!(
            scanned(pattern, &vec![b'a'; n]),
            (Ok(false), 11, 6 * n as u64)
        );
    }
    // A family of star shapes against texts with and without `/`: never
    // more than two readings of the text, one a star for the literal
    // scans, and 64 bytes a step.
    let atoms: [&[u8]; 12] = [
        b"*", b"**", b"**/", b"/", b"a", b"?", b"[!/]", b"*/", b"\\a", b"b", b"/**", b"*a",
    ];
    let texts = [
        vec![b'a'; 20_000],
        [vec![b'a'; 10_000], vec![b'/'; 10_000]].concat(),
        b"a/".repeat(10_000),
        [vec![b'/'; 10_000], vec![b'a'; 10_000]].concat(),
    ];
    let mut seed: u64 = 12_345;
    for _ in 0..500 {
        seed = seed
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        let pattern: Vec<u8> = (0..2 + (seed >> 60) as usize)
            .flat_map(|j| atoms[(seed >> (j * 4 % 56)) as usize % atoms.len()].to_vec())
            .collect();
        for text in &texts {
            let (_, taken, read) = scanned(&pattern, text);
            // A star's scan for its next literal byte reads the text once a
            // star, as the reference's does.
            let stars = pattern.iter().filter(|&&b| b == b'*').count() as u64;
            let bound = (stars + 2) * text.len() as u64 + 64 * (taken as u64 + 1);
            assert!(
                read <= bound,
                "{pattern:?}: {read} bytes read, bound {bound}"
            );
        }
    }
}

#[test]
fn a_budget_at_its_floor_is_spent() {
    for left in [i64::MIN, i64::MIN + 1, -1, 0] {
        let mut steps = Steps { left };
        assert_eq!(wildmatch(b"a", b"a", false, &mut steps), Err(Spent::Steps));
        assert!(steps.left < 0);
        // An empty pattern takes no step and matches an empty text.
        let mut steps = Steps { left };
        assert_eq!(wildmatch(b"", b"", false, &mut steps), Ok(true));
        assert_eq!(steps.left, left);
    }
}

#[test]
fn the_deepest_nesting_fits_a_small_stack() {
    // A Windows thread's stack can be 1 MiB; the deepest match the budget
    // allows, and those refused for depth, run in half that, unoptimised.
    let worker = std::thread::Builder::new()
        .stack_size(512 << 10)
        .spawn(|| {
            let deepest = run(&b"*a".repeat(WM_DEPTH), &b"a".repeat(WM_DEPTH), false);
            let past = run(
                &b"*a".repeat(WM_DEPTH + 1),
                &b"a".repeat(WM_DEPTH + 1),
                false,
            );
            let failing = run(
                &[b"*a".repeat(WM_DEPTH), b"b".to_vec()].concat(),
                &b"a".repeat(WM_DEPTH),
                false,
            );
            (deepest, past, failing)
        })
        .expect("a thread");
    let (deepest, past, failing) = worker.join().expect("no overflow");
    // The reference: 450 steps to match at 150 stars, spent by depth at
    // 151 after 452, and no match after 451.
    assert_eq!(deepest, (Ok(true), 450));
    assert_eq!(past, (Err(Spent::Depth), 452));
    assert_eq!(failing, (Ok(false), 451));
}
