use bettermemory_core::githead::signature::{First, Signature};
use bettermemory_core::origin_cache::{CaptureSignature, OriginEntry};
use bettermemory_core::origin_model::CapturedOrigin;
use bettermemory_core::unicode::cps;

pub fn entry(token: &str) -> OriginEntry {
    let mut origin = CapturedOrigin::default();
    origin.origin.repo = Some(cps(token));
    OriginEntry {
        origin,
        alternates: vec![cps(token)],
        captured_at: 0.0,
        captured_wall: 0.0,
        signature: CaptureSignature {
            base: Signature {
                first: First::Outside,
                head: None,
                config: None,
                head_file: None,
                refs: None,
                packed: None,
                git_dir: None,
                git_work_tree: None,
                ceilings: None,
            },
            extra: None,
        },
    }
}
