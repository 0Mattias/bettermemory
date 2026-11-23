//! Witness tallying and evidence strength from the membership resolution ladder.
pub mod tally;

use std::collections::BTreeSet;

use crate::capture::location::Location;
pub use crate::membership::history::{HistoryOrder, NativeHistory, TallyHistory};
use crate::membership::shared_heal::{token_error, MembershipLog};
use crate::membership::{Error, Membership, Query, Result2};
use crate::membership_records::records::Checkout;
use crate::pyvalue::{PyStr, PyValue};

pub type Held = Vec<(PyStr, BTreeSet<PyStr>)>;

/// The reads performed by a single tally inside its caller's transaction.
/// The held and rewritten answers retain the model's observed iteration order.
pub trait TallyModel {
    type Error;
    fn held(
        &mut self,
        loc: &Location,
        history: Option<&dyn TallyHistory<Self::Error>>,
        exclude: &[PyStr],
        order: &HistoryOrder,
    ) -> Result2<Held, Self::Error>;
    fn rewritten(
        &mut self,
        loc: &Location,
        history: Option<&dyn TallyHistory<Self::Error>>,
        exclude: &[PyStr],
        order: &HistoryOrder,
    ) -> Result2<Vec<(PyStr, i64)>, Self::Error>;
    fn rows(&mut self, table: &str, query: &Query) -> Result2<Vec<PyValue>, Self::Error>;
    fn canon(&mut self, p: &[u32]) -> Result2<PyStr, Self::Error>;
    fn checkout(&mut self, k: &[u32]) -> Result2<Checkout, Self::Error>;
    fn checkouts_with_token(&mut self, token: &[u32]) -> Result2<Vec<Checkout>, Self::Error>;
    fn file_binding(&mut self, id: &[u32]) -> Result2<Option<PyStr>, Self::Error>;
    fn members(&mut self, p: &[u32]) -> Result2<Vec<PyStr>, Self::Error>;
    fn project_tenant(&mut self, p: &[u32]) -> Result2<Option<PyStr>, Self::Error>;
    fn name(&mut self, p: &[u32]) -> Result2<PyStr, Self::Error>;
    fn has_witnesses(&mut self, p: &[u32]) -> Result2<bool, Self::Error>;
    fn view_state(&mut self, k: &Checkout) -> Result2<PyStr, Self::Error>;
    fn live_view(&mut self, k: &Checkout) -> Result2<Option<PyStr>, Self::Error>;
    fn retired_into(&mut self, k: &Checkout) -> Result2<Option<PyStr>, Self::Error>;
    fn view_made(&mut self, k: &Checkout, found: Option<&PyStr>) -> Result2<bool, Self::Error>;
    fn holds_objects(
        &mut self,
        history: &dyn TallyHistory<Self::Error>,
        ids: &[PyStr],
    ) -> Result2<BTreeSet<PyStr>, Self::Error> {
        history.holds_objects(ids)
    }
}

impl<S: MembershipLog> TallyModel for Membership<S> {
    type Error = S::Error;
    fn held(
        &mut self,
        loc: &Location,
        history: Option<&dyn TallyHistory<Self::Error>>,
        exclude: &[PyStr],
        order: &HistoryOrder,
    ) -> Result2<Held, Self::Error> {
        self.held_in_order(loc, history, exclude, order)
    }
    fn rewritten(
        &mut self,
        loc: &Location,
        history: Option<&dyn TallyHistory<Self::Error>>,
        exclude: &[PyStr],
        order: &HistoryOrder,
    ) -> Result2<Vec<(PyStr, i64)>, Self::Error> {
        self.rewritten_in_order(loc, history, exclude, order)
    }
    fn rows(&mut self, table: &str, query: &Query) -> Result2<Vec<PyValue>, Self::Error> {
        self.store()
            .membership_rows(table, query)
            .map_err(Error::Store)
    }
    fn canon(&mut self, p: &[u32]) -> Result2<PyStr, Self::Error> {
        Membership::canon(self, p)
    }
    fn checkout(&mut self, k: &[u32]) -> Result2<Checkout, Self::Error> {
        Membership::checkout(self, k)
    }
    fn checkouts_with_token(&mut self, token: &[u32]) -> Result2<Vec<Checkout>, Self::Error> {
        self.checkouts(Some(token))
    }
    fn file_binding(&mut self, id: &[u32]) -> Result2<Option<PyStr>, Self::Error> {
        self.file_binding_read(id)
    }
    fn members(&mut self, p: &[u32]) -> Result2<Vec<PyStr>, Self::Error> {
        Membership::members(self, p)
    }
    fn project_tenant(&mut self, p: &[u32]) -> Result2<Option<PyStr>, Self::Error> {
        Ok(self.project(p)?.tenant)
    }
    fn name(&mut self, p: &[u32]) -> Result2<PyStr, Self::Error> {
        Membership::name(self, p)
    }
    fn has_witnesses(&mut self, p: &[u32]) -> Result2<bool, Self::Error> {
        Membership::has_witnesses(self, p)
    }
    fn view_state(&mut self, k: &Checkout) -> Result2<PyStr, Self::Error> {
        Ok(Membership::view_state(self, k, None))
    }
    fn live_view(&mut self, k: &Checkout) -> Result2<Option<PyStr>, Self::Error> {
        Ok(Membership::live_view(self, k))
    }
    fn retired_into(&mut self, k: &Checkout) -> Result2<Option<PyStr>, Self::Error> {
        Membership::retired_into(self, k)
    }
    fn view_made(&mut self, k: &Checkout, found: Option<&PyStr>) -> Result2<bool, Self::Error> {
        crate::membership_shared::try_view_made(
            k,
            found,
            |tokens| self.carriers(tokens),
            |e| token_error(&e),
        )
    }
}
