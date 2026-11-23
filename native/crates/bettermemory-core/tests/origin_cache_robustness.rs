//! Large keys, non-finite clocks and cache churn.
mod origin_cache_cases;
use bettermemory_core::origin_cache::OriginCache;
use bettermemory_core::unicode::cps;
use origin_cache_cases::entry;

#[test]
fn nonfinite_clocks_and_surrogate_keys() {
    let mut cache = OriginCache::default();
    let key = vec![0xdcff; 1_000_000];
    cache.remember(key.clone(), entry("long"));
    let saved = cache.lookup(&key).unwrap();
    for clock in [
        f64::NAN,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::MAX,
        f64::MIN,
    ] {
        assert!(!saved.matches(clock, 0., &saved.signature));
        assert!(!saved.matches(0., clock, &saved.signature));
    }
    assert!(cache.lookup(&[0xdcff]).is_none());
    cache.remember(key.clone(), entry("new"));
    assert_eq!(
        cache.lookup(&key).unwrap().origin.origin.repo,
        Some(cps("new"))
    );
    cache.clear();
    assert!(cache.lookup(&key).is_none());
}
