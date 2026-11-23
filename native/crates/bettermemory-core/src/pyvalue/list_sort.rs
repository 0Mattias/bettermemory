//! CPython's stable list sort, preserving comparison order for partial orderings and errors.

type Compare<'a, T, E> = &'a dyn Fn(&T, &T) -> Result<bool, E>;

/// CPython 3.13's `count_run`: the length of the run at the start of
/// `a`, a descending one reversed in place.
fn count_run<T, E>(lt: Compare<'_, T, E>, a: &mut [T]) -> Result<usize, E> {
    let n = a.len();
    let lt = |a: &[T], i: usize, j: usize| lt(&a[i], &a[j]);
    let mut k = 1;
    while k < n && !lt(a, k, k - 1)? {
        k += 1;
    }
    if k == n {
        return Ok(n);
    }
    if k > 1 {
        if lt(a, 0, k - 1)? {
            return Ok(k);
        }
        a[..k].reverse();
    }
    k += 1;
    let mut neq = 0;
    while k < n {
        if lt(a, k, k - 1)? {
            if neq > 0 {
                a[k - neq - 1..k].reverse();
                neq = 0;
            }
        } else if lt(a, k - 1, k)? {
            break;
        } else {
            neq += 1;
        }
        k += 1;
    }
    if neq > 0 {
        a[k - neq - 1..k].reverse();
    }
    a[..k].reverse();
    while k < n && !lt(a, k, k - 1)? {
        k += 1;
    }
    Ok(k)
}

/// CPython 3.13's `binarysort`: `a[..ok]` sorted, the rest inserted.
fn binary_sort<T, E>(lt: Compare<'_, T, E>, a: &mut [T], ok: usize) -> Result<(), E> {
    for ok in ok.max(1)..a.len() {
        let (mut l, mut r) = (0, ok);
        loop {
            let m = (l + r) >> 1;
            if lt(&a[ok], &a[m])? {
                r = m;
            } else {
                l = m + 1;
            }
            if l >= r {
                break;
            }
        }
        a[l..=ok].rotate_right(1);
    }
    Ok(())
}

/// CPython's `MIN_GALLOP`.
const MIN_GALLOP: usize = 7;

/// CPython's `gallop_left`: where `key` goes in the sorted `a`, left of
/// any equal item, searched from `a[hint]` outwards.
fn gallop_left<T, E>(lt: Compare<'_, T, E>, key: &T, a: &[T], hint: usize) -> Result<usize, E> {
    let n = a.len();
    let (mut lastofs, mut ofs);
    if lt(&a[hint], key)? {
        let maxofs = n - hint;
        (lastofs, ofs) = (0, 1);
        while ofs < maxofs {
            if lt(&a[hint + ofs], key)? {
                lastofs = ofs;
                ofs = (ofs << 1) + 1;
            } else {
                break;
            }
        }
        ofs = ofs.min(maxofs);
        lastofs += hint;
        ofs += hint;
    } else {
        let maxofs = hint + 1;
        (lastofs, ofs) = (0, 1);
        while ofs < maxofs {
            if lt(&a[hint - ofs], key)? {
                break;
            }
            lastofs = ofs;
            ofs = (ofs << 1) + 1;
        }
        ofs = ofs.min(maxofs);
        // From offsets below `hint` to offsets from the start, one past
        // CPython's (whose `lastofs` may be -1).
        let k = lastofs;
        lastofs = hint + 1 - ofs;
        ofs = hint - k;
        return gallop_finish_left(lt, key, a, lastofs, ofs);
    }
    gallop_finish_left(lt, key, a, lastofs + 1, ofs)
}

/// The binary search that ends `gallop_left`, over `a[lo..hi]` with
/// `a[lo - 1] < key <= a[hi]`.
fn gallop_finish_left<T, E>(
    lt: Compare<'_, T, E>,
    key: &T,
    a: &[T],
    mut lo: usize,
    mut hi: usize,
) -> Result<usize, E> {
    while lo < hi {
        let m = lo + ((hi - lo) >> 1);
        if lt(&a[m], key)? {
            lo = m + 1;
        } else {
            hi = m;
        }
    }
    Ok(hi)
}

/// CPython's `gallop_right`: where `key` goes in the sorted `a`, right
/// of any equal item, searched from `a[hint]` outwards.
fn gallop_right<T, E>(lt: Compare<'_, T, E>, key: &T, a: &[T], hint: usize) -> Result<usize, E> {
    let n = a.len();
    let (mut lastofs, mut ofs) = (0, 1);
    let lo;
    if lt(key, &a[hint])? {
        let maxofs = hint + 1;
        while ofs < maxofs {
            if lt(key, &a[hint - ofs])? {
                lastofs = ofs;
                ofs = (ofs << 1) + 1;
            } else {
                break;
            }
        }
        ofs = ofs.min(maxofs);
        let k = lastofs;
        // One past CPython's `lastofs`, which may be -1.
        lo = hint + 1 - ofs;
        ofs = hint - k;
    } else {
        let maxofs = n - hint;
        while ofs < maxofs {
            if lt(key, &a[hint + ofs])? {
                break;
            }
            lastofs = ofs;
            ofs = (ofs << 1) + 1;
        }
        ofs = ofs.min(maxofs);
        lo = lastofs + hint + 1;
        ofs += hint;
    }
    let (mut lo, mut hi) = (lo, ofs);
    while lo < hi {
        let m = lo + ((hi - lo) >> 1);
        if lt(key, &a[m])? {
            hi = m;
        } else {
            lo = m + 1;
        }
    }
    Ok(hi)
}

/// CPython's `powerloop`: the power of the boundary between the run of
/// `n1` items at `s1` and the next one of `n2`, in a list of `n`.
fn powerloop(s1: usize, n1: usize, n2: usize, n: usize) -> u32 {
    let mut result = 0;
    let mut a = 2 * s1 + n1;
    let mut b = a + n1 + n2;
    loop {
        result += 1;
        if a >= n {
            a -= n;
            b -= n;
        } else if b >= n {
            break;
        }
        a <<= 1;
        b <<= 1;
    }
    result
}

/// A pending run: where it starts, its length, its power.
struct Run {
    base: usize,
    len: usize,
    power: u32,
}

/// CPython 3.13's `MergeState`, over the list being sorted.
struct Merger<'a, T, E> {
    lt: Compare<'a, T, E>,
    a: &'a mut [T],
    runs: Vec<Run>,
    min_gallop: usize,
}

impl<T: Clone, E> Merger<'_, T, E> {
    /// `found_new_run`: merge what the new run of `n2` items closes off.
    fn found_new_run(&mut self, n2: usize) -> Result<(), E> {
        if let Some(last) = self.runs.last() {
            let power = powerloop(last.base, last.len, n2, self.a.len());
            while self.runs.len() > 1 && self.runs[self.runs.len() - 2].power > power {
                self.merge_at(self.runs.len() - 2)?;
            }
            let last = self.runs.len() - 1;
            self.runs[last].power = power;
        }
        Ok(())
    }

    /// `merge_force_collapse`.
    fn force_collapse(&mut self) -> Result<(), E> {
        while self.runs.len() > 1 {
            let mut n = self.runs.len() - 2;
            if n > 0 && self.runs[n - 1].len < self.runs[n + 1].len {
                n -= 1;
            }
            self.merge_at(n)?;
        }
        Ok(())
    }

    /// `merge_at`: runs `i` and `i + 1` as one.
    fn merge_at(&mut self, i: usize) -> Result<(), E> {
        let (mut pa, mut na) = (self.runs[i].base, self.runs[i].len);
        let (pb, nb) = (self.runs[i + 1].base, self.runs[i + 1].len);
        self.runs[i].len = na + nb;
        self.runs.remove(i + 1);
        let k = gallop_right(self.lt, &self.a[pb], &self.a[pa..pa + na], 0)?;
        pa += k;
        na -= k;
        if na == 0 {
            return Ok(());
        }
        let nb = gallop_left(self.lt, &self.a[pa + na - 1], &self.a[pb..pb + nb], nb - 1)?;
        if nb == 0 {
            return Ok(());
        }
        if na <= nb {
            self.merge_lo(pa, na, pb, nb)
        } else {
            self.merge_hi(pa, na, pb, nb)
        }
    }

    /// `merge_lo`: the left run (the shorter) copied aside and merged
    /// from the left. On a refusal every item is still in the list.
    fn merge_lo(&mut self, pa: usize, na: usize, pb: usize, nb: usize) -> Result<(), E> {
        let tmp: Vec<T> = self.a[pa..pa + na].to_vec();
        let (dest, ia, ib) = (pa, 0, pb);
        let mut c = Cursor {
            dest,
            ia,
            ib,
            na,
            nb,
        };
        let result = merge_lo_body(self.lt, self.a, &tmp, &mut c, &mut self.min_gallop);
        let Cursor {
            dest,
            ia,
            ib,
            na,
            nb,
        } = c;
        let a = &mut *self.a;
        match result {
            // CopyB: the rest of the right run, then the left's last item.
            Ok(true) => {
                for j in 0..nb {
                    a[dest + j] = a[ib + j].clone();
                }
                a[dest + nb] = tmp[ia].clone();
                Ok(())
            }
            done => {
                a[dest..dest + na].clone_from_slice(&tmp[ia..ia + na]);
                done.map(|_| ())
            }
        }
    }

    /// `merge_hi`: the right run (the shorter) copied aside and merged
    /// from the right. Positions are one past CPython's pointers, which
    /// may point before the list.
    fn merge_hi(&mut self, pa: usize, na: usize, pb: usize, nb: usize) -> Result<(), E> {
        let tmp: Vec<T> = self.a[pb..pb + nb].to_vec();
        // `dest` and `ia` index the list, `ib` the copy, each one past
        // the item CPython's pointer names.
        let (dest, ia, ib) = (pb + nb, pa + na, nb);
        let mut c = Cursor {
            dest,
            ia,
            ib,
            na,
            nb,
        };
        let result = merge_hi_body(self.lt, self.a, &tmp, &mut c, &mut self.min_gallop);
        let Cursor {
            mut dest,
            ia,
            ib,
            na,
            nb,
        } = c;
        let a = &mut *self.a;
        match result {
            // CopyA: the rest of the left run, then the right's first item.
            Ok(true) => {
                for j in (0..na).rev() {
                    a[dest - na + j] = a[ia - na + j].clone();
                }
                dest -= na;
                a[dest - 1] = tmp[ib - 1].clone();
                Ok(())
            }
            done => {
                a[dest - nb..dest].clone_from_slice(&tmp[..nb]);
                done.map(|_| ())
            }
        }
    }
}

/// Where a merge stands: the next places to write and read, and how
/// many items each run has left.
struct Cursor {
    dest: usize,
    ia: usize,
    ib: usize,
    na: usize,
    nb: usize,
}

/// The merge loop of `merge_lo`; `Ok(true)` is its `CopyB` exit.
fn merge_lo_body<T: Clone, E>(
    lt: Compare<'_, T, E>,
    a: &mut [T],
    tmp: &[T],
    c: &mut Cursor,
    min_gallop_out: &mut usize,
) -> Result<bool, E> {
    a[c.dest] = a[c.ib].clone();
    c.dest += 1;
    c.ib += 1;
    c.nb -= 1;
    if c.nb == 0 {
        return Ok(false);
    }
    if c.na == 1 {
        return Ok(true);
    }
    let mut min_gallop = *min_gallop_out;
    loop {
        let (mut acount, mut bcount) = (0, 0);
        loop {
            if lt(&a[c.ib], &tmp[c.ia])? {
                a[c.dest] = a[c.ib].clone();
                c.dest += 1;
                c.ib += 1;
                bcount += 1;
                acount = 0;
                c.nb -= 1;
                if c.nb == 0 {
                    return Ok(false);
                }
                if bcount >= min_gallop {
                    break;
                }
            } else {
                a[c.dest] = tmp[c.ia].clone();
                c.dest += 1;
                c.ia += 1;
                acount += 1;
                bcount = 0;
                c.na -= 1;
                if c.na == 1 {
                    return Ok(true);
                }
                if acount >= min_gallop {
                    break;
                }
            }
        }
        min_gallop += 1;
        loop {
            min_gallop -= usize::from(min_gallop > 1);
            *min_gallop_out = min_gallop;
            let k = gallop_right(lt, &a[c.ib], &tmp[c.ia..c.ia + c.na], 0)?;
            acount = k;
            if k > 0 {
                a[c.dest..c.dest + k].clone_from_slice(&tmp[c.ia..c.ia + k]);
                c.dest += k;
                c.ia += k;
                c.na -= k;
                if c.na == 1 {
                    return Ok(true);
                }
                if c.na == 0 {
                    return Ok(false);
                }
            }
            a[c.dest] = a[c.ib].clone();
            c.dest += 1;
            c.ib += 1;
            c.nb -= 1;
            if c.nb == 0 {
                return Ok(false);
            }
            let k = gallop_left(lt, &tmp[c.ia], &a[c.ib..c.ib + c.nb], 0)?;
            bcount = k;
            if k > 0 {
                for j in 0..k {
                    a[c.dest + j] = a[c.ib + j].clone();
                }
                c.dest += k;
                c.ib += k;
                c.nb -= k;
                if c.nb == 0 {
                    return Ok(false);
                }
            }
            a[c.dest] = tmp[c.ia].clone();
            c.dest += 1;
            c.ia += 1;
            c.na -= 1;
            if c.na == 1 {
                return Ok(true);
            }
            if acount < MIN_GALLOP && bcount < MIN_GALLOP {
                break;
            }
        }
        min_gallop += 1;
        *min_gallop_out = min_gallop;
    }
}

/// The merge loop of `merge_hi`; `Ok(true)` is its `CopyA` exit.
fn merge_hi_body<T: Clone, E>(
    lt: Compare<'_, T, E>,
    a: &mut [T],
    tmp: &[T],
    c: &mut Cursor,
    min_gallop_out: &mut usize,
) -> Result<bool, E> {
    a[c.dest - 1] = a[c.ia - 1].clone();
    c.dest -= 1;
    c.ia -= 1;
    c.na -= 1;
    if c.na == 0 {
        return Ok(false);
    }
    if c.nb == 1 {
        return Ok(true);
    }
    let mut min_gallop = *min_gallop_out;
    loop {
        let (mut acount, mut bcount) = (0, 0);
        loop {
            if lt(&tmp[c.ib - 1], &a[c.ia - 1])? {
                a[c.dest - 1] = a[c.ia - 1].clone();
                c.dest -= 1;
                c.ia -= 1;
                acount += 1;
                bcount = 0;
                c.na -= 1;
                if c.na == 0 {
                    return Ok(false);
                }
                if acount >= min_gallop {
                    break;
                }
            } else {
                a[c.dest - 1] = tmp[c.ib - 1].clone();
                c.dest -= 1;
                c.ib -= 1;
                bcount += 1;
                acount = 0;
                c.nb -= 1;
                if c.nb == 1 {
                    return Ok(true);
                }
                if bcount >= min_gallop {
                    break;
                }
            }
        }
        min_gallop += 1;
        loop {
            min_gallop -= usize::from(min_gallop > 1);
            *min_gallop_out = min_gallop;
            let k = gallop_right(lt, &tmp[c.ib - 1], &a[c.ia - c.na..c.ia], c.na - 1)?;
            let k = c.na - k;
            acount = k;
            if k > 0 {
                for j in (0..k).rev() {
                    a[c.dest - k + j] = a[c.ia - k + j].clone();
                }
                c.dest -= k;
                c.ia -= k;
                c.na -= k;
                if c.na == 0 {
                    return Ok(false);
                }
            }
            a[c.dest - 1] = tmp[c.ib - 1].clone();
            c.dest -= 1;
            c.ib -= 1;
            c.nb -= 1;
            if c.nb == 1 {
                return Ok(true);
            }
            let k = gallop_left(lt, &a[c.ia - 1], &tmp[..c.nb], c.nb - 1)?;
            let k = c.nb - k;
            bcount = k;
            if k > 0 {
                a[c.dest - k..c.dest].clone_from_slice(&tmp[c.ib - k..c.ib]);
                c.dest -= k;
                c.ib -= k;
                c.nb -= k;
                if c.nb == 1 {
                    return Ok(true);
                }
                if c.nb == 0 {
                    return Ok(false);
                }
            }
            a[c.dest - 1] = a[c.ia - 1].clone();
            c.dest -= 1;
            c.ia -= 1;
            c.na -= 1;
            if c.na == 0 {
                return Ok(false);
            }
            if acount < MIN_GALLOP && bcount < MIN_GALLOP {
                break;
            }
        }
        min_gallop += 1;
        *min_gallop_out = min_gallop;
    }
}

/// `list.sort` over the supplied items: ascending, stable, by CPython 3.13's
/// own sequence of comparisons (`count_run`, binary insertion up to the
/// minimum run, the powersort merge order, galloping merges), so a
/// TypeError names the pair CPython's first failing comparison names and
/// a NaN leaves the order CPython leaves.
pub(crate) fn sort_by<T: Clone, E>(
    a: &mut [T],
    compare: impl Fn(&T, &T) -> Result<bool, E>,
) -> Result<(), E> {
    let lt: Compare<'_, T, E> = &compare;
    let n = a.len();
    if n < 2 {
        return Ok(());
    }
    let min_run = {
        let (mut n, mut r) = (n, 0);
        while n >= 64 {
            r |= n & 1;
            n >>= 1;
        }
        n + r
    };
    let mut merger = Merger {
        lt,
        a,
        runs: Vec::new(),
        min_gallop: MIN_GALLOP,
    };
    let mut lo = 0;
    while lo < n {
        let mut k = count_run(lt, &mut merger.a[lo..])?;
        if k < min_run {
            let force = (n - lo).min(min_run);
            binary_sort(lt, &mut merger.a[lo..lo + force], k)?;
            k = force;
        }
        merger.found_new_run(k)?;
        merger.runs.push(Run {
            base: lo,
            len: k,
            power: 0,
        });
        lo += k;
    }
    merger.force_collapse()
}
