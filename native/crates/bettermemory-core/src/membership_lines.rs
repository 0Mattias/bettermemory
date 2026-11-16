//! `membership_lines` (660 lines at 851c171d), less the restated question:
//! the lines shown to a user about which project a folder belongs to, as
//! functions of their arguments. Ported: `folder_said`, `run_in`, `_named`,
//! `_answer` and L1 to L10 with `line_unresolved` (66-405), and the lines
//! without a number (597-660). Left for the next unit: `REASON_SAID`,
//! `REASON_UNNAMED` and `line_restated` (407-594).
//!
//! Every text a folder, a repository or a document can choose goes
//! through [`quoted`]; ids, counts and composed text through
//! [`printable`], as the reference writes them. An optional text the
//! reference tests by truth (`if question:`) is absent when empty too;
//! one it tests against None (`folder`, `ref`, `machine`, `path`) is not.

use crate::capture::{str_of, STR_DEPTH_TOP};
use crate::printable::{printable_into, quoted_into};
use crate::pyvalue::{PyError, PyStr, PyValue};

/// A project a line names: (name, id), as `line_first_sight` takes them.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Candidate {
    pub name: PyStr,
    pub id: PyStr,
}

/// A checkout seen at a path before (`line_new`'s `previous`): (checkout,
/// project name, project id).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Previous {
    pub checkout: PyStr,
    pub name: PyStr,
    pub id: PyStr,
}

/// An import's candidate (`line_import`): (name, id, witness codes).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ImportCandidate {
    pub name: PyStr,
    pub id: PyStr,
    pub codes: Vec<PyStr>,
}

/// `membership_records.WORDS`: how a line names each witness code.
pub const WORDS: [(&str, &str); 11] = [
    ("O", "is its git directory"),
    ("S", "shares its object store"),
    ("H", "holds its commits"),
    (
        "Hf",
        "holds its commits rewritten (same authors, dates and messages)",
    ),
    ("Hb", "holds the file contents of its commits"),
    ("R", "names its remote"),
    ("D", "names a remote you declared for it"),
    ("F", "carries its project file"),
    ("T", "carries its checkout token"),
    ("B", "sits where its checkout was, with its commits"),
    ("C", "has its folder name and the same files and sizes"),
];

fn word(code: &[u32]) -> Option<&'static str> {
    WORDS
        .iter()
        .find(|(k, _)| k.len() == code.len() && k.bytes().map(u32::from).eq(code.iter().copied()))
        .map(|(_, w)| *w)
}

/// A line being written.
struct Out(PyStr);

impl Out {
    fn new() -> Out {
        Out(Vec::new())
    }
    fn s(&mut self, text: &str) -> &mut Out {
        self.0.extend(text.chars().map(u32::from));
        self
    }
    fn raw(&mut self, text: &[u32]) -> &mut Out {
        self.0.extend_from_slice(text);
        self
    }
    fn q(&mut self, text: &[u32]) -> &mut Out {
        quoted_into(text, &mut self.0);
        self
    }
    fn p(&mut self, text: &[u32]) -> &mut Out {
        printable_into(text, &mut self.0);
        self
    }
    fn n(&mut self, value: i64) -> &mut Out {
        self.s(&value.to_string())
    }
    fn named(&mut self, name: &[u32], id: &[u32]) -> &mut Out {
        self.q(name).s(" (").p(id).s(")")
    }
    fn answer(
        &mut self,
        question: Option<&[u32]>,
        answer: &[u32],
        target: Option<&[u32]>,
        said: bool,
    ) -> &mut Out {
        self.s("memory_admin(action=\"answer_question\"");
        if let Some(q) = truthy(question) {
            self.s(", id=").q(q);
        }
        self.s(", answer=").q(answer);
        if let Some(t) = truthy(target) {
            self.s(", target=").q(t);
        }
        if said {
            self.s(", said=\"<the user's words>\"");
        }
        self.s(")")
    }
    fn folder_said(&mut self, folder: Option<&[u32]>, reference: Option<&[u32]>) -> &mut Out {
        self.s("the stored folder (");
        if let Some(f) = folder {
            self.s("display only: ").q(f).s("; ");
        }
        match reference {
            Some(r) if is_reference(r) => {
                self.s("read bettermemory project folder ")
                    .raw(r)
                    .s(" and use its JSON path");
            }
            _ => {
                self.s("use its stored path");
            }
        }
        self.s(
            " as the command's working directory, never paste the displayed \
             name into a shell)",
        )
    }
    fn run_in(&mut self, folder: Option<&[u32]>, reference: Option<&[u32]>) -> &mut Out {
        self.s("in ").folder_said(folder, reference)
    }
    /// `sep.join(...)` over `items`, each written by `each`.
    fn join<T>(&mut self, sep: &str, items: &[T], mut each: impl FnMut(&mut Out, &T)) -> &mut Out {
        for (i, item) in items.iter().enumerate() {
            if i > 0 {
                self.s(sep);
            }
            each(self, item);
        }
        self
    }
    fn words(&mut self, codes: &[PyStr]) -> &mut Out {
        let mut sorted: Vec<&PyStr> = codes.iter().collect();
        sorted.sort();
        let found: Vec<&str> = sorted.iter().filter_map(|c| word(c)).collect();
        self.join(", ", &found, |o, w| {
            o.s(w);
        })
    }
    fn done(&mut self) -> PyStr {
        std::mem::take(&mut self.0)
    }
}

fn truthy(text: Option<&[u32]>) -> Option<&[u32]> {
    text.filter(|t| !t.is_empty())
}

/// `_REFERENCE.fullmatch(ref)`: `[A-Za-z0-9_]+`.
fn is_reference(text: &[u32]) -> bool {
    !text.is_empty()
        && text
            .iter()
            .all(|&c| c < 0x80 && ((c as u8).is_ascii_alphanumeric() || c == u32::from(b'_')))
}

/// `folder_said(folder, ref)` (75-92).
pub fn folder_said(folder: Option<&[u32]>, reference: Option<&[u32]>) -> PyStr {
    Out::new().folder_said(folder, reference).done()
}

/// `run_in(folder, ref)` (95-97).
pub fn run_in(folder: Option<&[u32]>, reference: Option<&[u32]>) -> PyStr {
    Out::new().run_in(folder, reference).done()
}

/// `_named(name, project_id)` (105-108).
pub fn named(name: &[u32], project_id: &[u32]) -> PyStr {
    Out::new().named(name, project_id).done()
}

/// `_answer(question, answer, target, said=said)` (111-126).
pub fn answer(
    question: Option<&[u32]>,
    answer: &[u32],
    target: Option<&[u32]>,
    said: bool,
) -> PyStr {
    Out::new().answer(question, answer, target, said).done()
}

const JOIN: &[u32] = &[0x6A, 0x6F, 0x69, 0x6E];
const SEPARATE: &[u32] = &[0x73, 0x65, 0x70, 0x61, 0x72, 0x61, 0x74, 0x65];

/// L1, `line_first_sight` (129-157).
pub fn line_first_sight(
    root: &[u32],
    candidates: &[Candidate],
    evidence: &[u32],
    unwritten: Option<&[u32]>,
    filed: bool,
    question: Option<&[u32]>,
) -> PyStr {
    let mut o = Out::new();
    o.s("bettermemory: ")
        .q(root)
        .s(" may be project ")
        .join(" or ", candidates, |o, c| {
            o.named(&c.name, &c.id);
        })
        .s(": ")
        .p(evidence);
    if let Some(u) = truthy(unwritten) {
        o.s(" (could not write ").p(u).s(")");
    }
    o.s(
        ". Until answered, nothing from other projects is shown here and what \
         is written here stays with this folder. Same project, when the user says so: ",
    )
    .join(" | ", candidates, |o, c| {
        o.answer(question, JOIN, Some(&c.id), true);
    })
    .s("; separate: ");
    if filed {
        o.s("bettermemory project init --new (")
            .run_in(Some(root), question)
            .s(", a fresh project file)");
    } else {
        o.answer(question, SEPARATE, None, false);
    }
    o.done()
}

/// L2, `line_new` (160-186).
pub fn line_new(
    root: &[u32],
    name: &[u32],
    project_id: &[u32],
    previous: &[Previous],
    unwritten: Option<&[u32]>,
) -> PyStr {
    let mut o = Out::new();
    o.s("bettermemory: ")
        .q(root)
        .s(" is a new project ")
        .named(name, project_id);
    if !previous.is_empty() {
        o.s("; ")
            .join(", ", previous, |o, k| {
                o.s("checkout ")
                    .p(&k.checkout)
                    .s(" of ")
                    .named(&k.name, &k.id);
            })
            .s(
                " was here before and this repository is not it (no token, commit or \
                file of it); if it is: ",
            )
            .join(" | ", previous, |o, k| {
                o.s("bettermemory project join ").p(&k.id);
            });
    }
    if let Some(u) = truthy(unwritten) {
        o.s("; could not write its token (").p(u).s(")");
    }
    o.done()
}

/// L3, `line_file_contradiction` (189-204).
pub fn line_file_contradiction(
    root: &[u32],
    name: &[u32],
    project_id: &[u32],
    file_name: &[u32],
    file_project: &[u32],
    question: Option<&[u32]>,
) -> PyStr {
    Out::new()
        .s("bettermemory: ")
        .q(root)
        .s(" is project ")
        .named(name, project_id)
        .s(" but its project file names ")
        .named(file_name, file_project)
        .s(". Keep: bettermemory project init --rebind (")
        .run_in(Some(root), question)
        .s("); move this checkout, when the user says so: ")
        .answer(question, JOIN, Some(file_project), true)
        .done()
}

/// L4, `line_joined` (207-214): the words of `codes` in sorted order,
/// codes `WORDS` does not hold left out, a repeated code repeated.
pub fn line_joined(root: &[u32], name: &[u32], project_id: &[u32], codes: &[PyStr]) -> PyStr {
    Out::new()
        .s("bettermemory: ")
        .q(root)
        .s(" is project ")
        .named(name, project_id)
        .s(" (")
        .words(codes)
        .s("); what was written here joined it")
        .done()
}

/// L4 for an alias clone's project merged, `line_alias_joined` (217-237).
pub fn line_alias_joined(
    root: &[u32],
    name: &[u32],
    project_id: &[u32],
    merged: &[u32],
    merged_id: &[u32],
    codes: &[PyStr],
) -> PyStr {
    Out::new()
        .s("bettermemory: ")
        .q(root)
        .s(" is project ")
        .named(name, project_id)
        .s(" (")
        .words(codes)
        .s("); its project ")
        .named(merged, merged_id)
        .s(
            ", the same repository under a remote alias, joined it with what was written \
            there. Undo, when the user says so: memory_admin(action=\"unjoin_project\", id=",
        )
        .q(merged_id)
        .s("); what is written here until then stays with ")
        .named(name, project_id)
        .done()
}

/// L4 held, `line_alias_held` (240-271). `machine` and `path` are the
/// reference's `where` pair.
#[allow(clippy::too_many_arguments)]
pub fn line_alias_held(
    root: &[u32],
    name: &[u32],
    project_id: &[u32],
    target: &[u32],
    target_id: &[u32],
    question: &[u32],
    checkout: &[u32],
    machine: Option<&[u32]>,
    path: Option<&[u32]>,
    shown: &[u32],
) -> PyStr {
    const PREFIX: &str = "bettermemory: ";
    let prefix: Vec<u32> = PREFIX.chars().map(u32::from).collect();
    let said = shown.strip_prefix(prefix.as_slice()).unwrap_or(shown);
    let mut o = Out::new();
    o.s("bettermemory: ")
        .q(root)
        .s(": its project ")
        .named(name, project_id)
        .s(" stays apart from ")
        .named(target, target_id)
        .s(", whose remotes it fetches from, while question ")
        .p(question)
        .s(" is open on checkout ")
        .p(checkout);
    if let Some(m) = machine {
        o.s(" on machine ").q(m);
    }
    if let Some(p) = path {
        o.s(", last seen at ").q(p);
    }
    o.s("; answered here or there, it is closed: ")
        .p(said)
        .done()
}

/// L5, `line_remote_diverged` (274-291).
pub fn line_remote_diverged(
    root: &[u32],
    new_key: &[u32],
    name: &[u32],
    project_id: &[u32],
    old_key: &[u32],
    question: Option<&[u32]>,
) -> PyStr {
    Out::new()
        .s("bettermemory: ")
        .q(root)
        .s(" now names ")
        .q(new_key)
        .s(", while project ")
        .named(name, project_id)
        .s(" elsewhere still names ")
        .q(old_key)
        .s(". Still the same project, when the user says so: ")
        .answer(question, JOIN, Some(project_id), true)
        .s("; a project of its own: ")
        .answer(question, SEPARATE, None, false)
        .done()
}

/// L6, `line_legacy` (294-318): `recorded` None is `memories`.
#[allow(clippy::too_many_arguments)]
pub fn line_legacy(
    name: &[u32],
    memories: i64,
    episodes: i64,
    path: &[u32],
    held: i64,
    candidate_name: &[u32],
    candidate_id: &[u32],
    recorded: Option<i64>,
    question: Option<&[u32]>,
) -> PyStr {
    Out::new()
        .s("bettermemory: the records of ")
        .q(name)
        .s(" (")
        .n(memories)
        .s(" memories, ")
        .n(episodes)
        .s(" episodes; its folder ")
        .q(path)
        .s(" is gone) may belong to project ")
        .q(candidate_name)
        .s(" here: this repository holds ")
        .n(held)
        .s(" of their ")
        .n(recorded.unwrap_or(memories))
        .s(" recorded commits. Same project, when the user says so: ")
        .answer(question, JOIN, Some(candidate_id), true)
        .s("; separate: ")
        .answer(question, SEPARATE, None, false)
        .done()
}

/// L7, `line_subproject` (321-341).
pub fn line_subproject(
    file_path: &[u32],
    name: &[u32],
    project_id: &[u32],
    repository_name: &[u32],
    parent_name: &[u32],
    folder: &[u32],
    question: Option<&[u32]>,
) -> PyStr {
    Out::new()
        .s("bettermemory: ")
        .q(file_path)
        .s(" names ")
        .named(name, project_id)
        .s(", which is not part of ")
        .q(repository_name)
        .s("; treated as ")
        .q(parent_name)
        .s(" until answered. That folder is ")
        .named(name, project_id)
        .s(", when the user says so: ")
        .answer(question, JOIN, Some(project_id), true)
        .s("; a part of ")
        .q(parent_name)
        .s(" with an id of its own: bettermemory project init --new (")
        .run_in(Some(folder), question)
        .s(")")
        .done()
}

/// L8, `line_unreadable` (344-350).
pub fn line_unreadable(cwd: &[u32], detail: &[u32]) -> PyStr {
    Out::new()
        .s("bettermemory: cannot tell what ")
        .q(cwd)
        .s(" is (")
        .q(detail)
        .s("); nothing from any project is shown here")
        .done()
}

/// L9, `line_deferred` (353-360).
pub fn line_deferred(root: &[u32], file_name: &[u32]) -> PyStr {
    Out::new()
        .s("bettermemory: ")
        .q(root)
        .s(": ")
        .q(file_name)
        .s(
            " is not on this disk yet (evicted by the file provider); it was asked for; \
            this prompt shows nothing from any project and the next one decides",
        )
        .done()
}

/// L10, `line_import` (363-382).
pub fn line_import(
    name: &[u32],
    source: &[u32],
    candidates: &[ImportCandidate],
    question: Option<&[u32]>,
) -> PyStr {
    let first = candidates.first().map(|c| c.id.as_slice());
    Out::new()
        .s("bettermemory: imported project ")
        .q(name)
        .s(" from ")
        .q(source)
        .s(" may be ")
        .join(", ", candidates, |o, c| {
            let mut codes: Vec<&PyStr> = c.codes.iter().collect();
            codes.sort();
            let mut plus = Vec::new();
            for (i, code) in codes.iter().enumerate() {
                if i > 0 {
                    plus.push(u32::from(b'+'));
                }
                plus.extend_from_slice(code);
            }
            o.q(&c.name).s(" (").p(&c.id).s(": ").p(&plus).s(")");
        })
        .s(". Same project, when the user says so: ")
        .answer(question, JOIN, first, true)
        .s(" (in that checkout); keep apart: ")
        .answer(question, SEPARATE, None, false)
        .done()
}

/// L6 in decision B6's form, `line_unresolved` (385-404).
pub fn line_unresolved(
    path: &[u32],
    memories: i64,
    episodes: i64,
    name: &[u32],
    project_id: &[u32],
    question: Option<&[u32]>,
) -> PyStr {
    Out::new()
        .s("bettermemory: the records written at ")
        .q(path)
        .s(" while it could not be read (")
        .n(memories)
        .s(" memories, ")
        .n(episodes)
        .s(" episodes) may belong to project ")
        .named(name, project_id)
        .s(" here. Same project, when the user says so: ")
        .answer(question, JOIN, Some(project_id), true)
        .s("; a project of their own: ")
        .answer(question, SEPARATE, None, false)
        .done()
}

/// `line_folder_project` (602-609).
pub fn line_folder_project(folder: &[u32], name: &[u32], boundary: bool) -> PyStr {
    let mut o = Out::new();
    o.s("bettermemory: ")
        .q(folder)
        .s(" is a project of its own (")
        .q(name)
        .s(", its file)");
    if boundary {
        o.s("; a boundary: nothing crosses it");
    }
    o.done()
}

/// `line_new_folder` (612-617).
pub fn line_new_folder(root: &[u32], name: &[u32], project_id: &[u32]) -> PyStr {
    Out::new()
        .s("bettermemory: ")
        .q(root)
        .s(" is a new project ")
        .named(name, project_id)
        .s(", its file")
        .done()
}

/// B12, `line_boundary_added` (620-625).
pub fn line_boundary_added(path: &[u32], name: &[u32], project_id: &[u32]) -> PyStr {
    Out::new()
        .s("bettermemory: ")
        .q(path)
        .s(" now declares a boundary: ")
        .named(name, project_id)
        .s(" is a tenant from now on; nothing crosses it")
        .done()
}

/// B12, `line_boundary_dropped` (628-639).
pub fn line_boundary_dropped(
    path: &[u32],
    name: &[u32],
    project_id: &[u32],
    folder: &[u32],
    file_id: &[u32],
) -> PyStr {
    Out::new()
        .s("bettermemory: ")
        .q(path)
        .s(" no longer declares a boundary; ")
        .named(name, project_id)
        .s(" stays one until it is lifted: bettermemory project init --rebind (")
        .run_in(Some(folder), Some(file_id))
        .s(") binds a fresh file without it")
        .done()
}

/// `line_across_boundary` (642-649): `named` are `_named` spellings.
pub fn line_across_boundary(root: &[u32], named: &[PyStr]) -> PyStr {
    Out::new()
        .s("bettermemory: ")
        .q(root)
        .s(" matches ")
        .join(", ", named, |o, n| {
            o.p(n);
        })
        .s(" across a boundary; kept apart")
        .done()
}

/// `line_content_crowded` (652-660).
pub fn line_content_crowded(root: &[u32], count: i64, limit: i64) -> PyStr {
    Out::new()
        .s("bettermemory: ")
        .q(root)
        .s(" has the folder name and files of ")
        .n(count)
        .s(" commit-less projects with no live checkout, more than the ")
        .n(limit)
        .s(
            " a question names, so none is offered; `bettermemory project list` shows \
            them, `bettermemory project join <id>` joins one",
        )
        .done()
}

// ---------------------------------------------------------------------------
// The restated question (407-595)
// ---------------------------------------------------------------------------

/// The question kinds (`membership_records`, design 2.1 and 3.4).
pub const FIRST_SIGHT: &str = "first_sight";
pub const FILE_CONTRADICTION: &str = "file_contradiction";
pub const REMOTE_DIVERGED: &str = "remote_diverged";
pub const SUBPROJECT_CONTRADICTION: &str = "subproject_contradiction";
pub const DUPLICATE_CHECKOUT: &str = "duplicate_checkout";
pub const LEGACY: &str = "legacy";
pub const IMPORT: &str = "import";

/// `REASON_SAID` (416-475): the sentence a restated question says for
/// each reason, in the reference's order.
pub const REASON_SAID: [(&str, &str); 16] = [
    ("several_projects", "the evidence of more than one project holds here and none outranks the others"),
    ("one_witness", "one witness names it here, and one alone does not join"),
    ("content_only", "only the folder's name and files match it, which do not join alone"),
    ("a3_history_join", "this repository holds its history, and under membership.strict a join resting on history asks"),
    ("token_shared_no_commit", "this checkout carries the checkout token of a live checkout with which it has no commit in common (a template's sibling, or a planted token), so the token is not evidence of the project"),
    ("file_names_other_history", "its project file names one project while it holds the commits of another and none of the named one's"),
    ("boundary", "its project file names a project of another boundary"),
    ("file_contradiction", "its project file names another project than the checkout's own"),
    ("remote_diverged", "its remote changed while the project elsewhere still names the old remote"),
    ("subproject_contradiction", "a project file below the checkout's root names a project that is not part of the project around that folder, and the folder is treated as the project around it until answered"),
    ("duplicate_checkout", "this checkout is a second record of a git directory another machine shares, registered by a sighting that did not read that machine's re-mint of the token as the first record's, while the first record is in the first project named; what was written here since stays in this checkout's project until answered"),
    ("legacy_no_live_checkout", "the records of a project whose folder is gone may belong to it: this repository holds some of their recorded commits"),
    ("unresolved_location", "records written at a folder that could not be read may belong to it"),
    ("import_one_witness", "an imported project has one witness here, and one alone does not join"),
    ("import_contradiction", "an imported project's file names one project here while another holds its commits"),
    ("import_several_projects", "an imported project's evidence names more than one project here"),
];

/// `REASON_UNNAMED` (478): said for a reason the table does not hold.
pub const REASON_UNNAMED: &str = "a reason this version does not name";

/// A candidate of a restated question: its project's name, None when the
/// project is gone, and its id (merges followed by the caller).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Named {
    pub name: Option<PyStr>,
    pub id: PyStr,
}

fn eq(text: &[u32], word: &str) -> bool {
    text.len() == word.len() && word.bytes().map(u32::from).eq(text.iter().copied())
}

/// `str.replace(old, new)`: every occurrence, left to right, none
/// overlapping (`old` is never empty here).
fn replace(text: &[u32], old: &[u32], new: &[u32]) -> PyStr {
    let mut out = Vec::with_capacity(text.len());
    let mut i = 0;
    while i < text.len() {
        if text[i..].starts_with(old) {
            out.extend_from_slice(new);
            i += old.len();
        } else {
            out.push(text[i]);
            i += 1;
        }
    }
    out
}

fn cps(text: &str) -> PyStr {
    text.chars().map(u32::from).collect()
}

/// The sentence `REASON_SAID` holds for `reason`, else `REASON_UNNAMED`.
pub fn reason_said(reason: &[u32]) -> &'static str {
    REASON_SAID
        .iter()
        .find(|(k, _)| eq(reason, k))
        .map_or(REASON_UNNAMED, |(_, s)| *s)
}

/// `line_restated` (481-595): a question shown again from what its row
/// stores. `kind` and `reason` choose fixed text and are never shown;
/// the question's id and a gone candidate's id go through `printable`, a
/// candidate's name, the folder and `at` through `quoted`.
pub fn line_restated(
    question: &[u32],
    kind: &[u32],
    reason: &[u32],
    candidates: &[Named],
    filed: bool,
    folder: Option<&[u32]>,
    at: Option<&[u32]>,
) -> PyStr {
    let q = Some(question);
    let mut names = Out::new();
    names.join(" or ", candidates, |o, c| match &c.name {
        Some(n) => {
            o.named(n, &c.id);
        }
        None => {
            o.p(&c.id);
        }
    });
    let mut names = names.done();
    if names.is_empty() {
        names = cps("no project");
    }
    let mut said = cps(reason_said(reason));
    let mut joins = Out::new();
    joins.join(" | ", candidates, |o, c| {
        o.answer(q, JOIN, Some(&c.id), true);
    });
    let mut joins = joins.done();
    if joins.is_empty() {
        joins = cps("(no project to join)");
    }
    let mut separate = Out::new().answer(q, SEPARATE, None, false).done();
    let here = match at {
        Some(a) => Out::new().s("in ").q(a).done(),
        None => cps("here"),
    };
    let there = Out::new().s(", run ").run_in(at, q).done();
    let this = if at.is_some() { "that" } else { "this" };
    if at.is_some() {
        said = replace(&said, &cps("this checkout"), &cps("that checkout"));
        said = replace(&said, &cps("this repository"), &cps("that repository"));
        let mut with = vec![u32::from(b' ')];
        with.extend_from_slice(&here);
        said = replace(&said, &cps(" here"), &with);
    }
    let mut o = Out::new();
    if eq(kind, FILE_CONTRADICTION) {
        o.s("Keep: bettermemory project init --rebind")
            .raw(&there)
            .s("; move ")
            .s(this)
            .s(" checkout, when the user says so: ")
            .raw(&joins);
    } else if eq(kind, SUBPROJECT_CONTRADICTION) {
        let folder = folder.filter(|f| !f.is_empty());
        o.s("That folder is that project, when the user says so: ")
            .raw(&joins)
            .s("; a part with an id of its own: bettermemory project init --new (in ")
            .folder_said(folder, q)
            .s(")");
    } else if eq(kind, REMOTE_DIVERGED) {
        o.s("Still the same project, when the user says so: ")
            .raw(&joins)
            .s("; a project of its own: ")
            .raw(&separate);
    } else if eq(kind, DUPLICATE_CHECKOUT) {
        let pick = |i: usize| match candidates.get(i) {
            Some(c) => Out::new().answer(q, JOIN, Some(&c.id), true).done(),
            None => cps("(no project to join)"),
        };
        o.s("The first record's project, ")
            .s(this)
            .s(" checkout and what was written ")
            .raw(&here)
            .s(" moving there, when the user says so: ")
            .raw(&pick(0))
            .s("; ")
            .s(this)
            .s(" checkout's own, where it is, when the user says so: ")
            .raw(&pick(1))
            .s("; a new project of its own, with what was written ")
            .raw(&here)
            .s(": ")
            .raw(&separate);
    } else if eq(kind, IMPORT) {
        o.s("Same project, when the user says so: ")
            .raw(&joins)
            .s(" (in that checkout); keep apart: ")
            .raw(&separate);
    } else {
        if eq(kind, FIRST_SIGHT) && at.is_some() {
            separate = Out::new()
                .raw(&separate)
                .s(" (refused where the project file ")
                .raw(&here)
                .s(" is bound to another project: then bettermemory project init --new")
                .raw(&there)
                .s(", a fresh project file)")
                .done();
        } else if eq(kind, FIRST_SIGHT) && filed {
            separate = Out::new()
                .s("bettermemory project init --new (")
                .run_in(None, q)
                .s(", a fresh project file)")
                .done();
        }
        o.s("Same project, when the user says so: ")
            .raw(&joins)
            .s("; separate: ")
            .raw(&separate);
    }
    let options = o.done();
    let mut line = Out::new();
    line.s("bettermemory: question ")
        .p(question)
        .s(", restated from what it stores, asks about project ")
        .raw(&names)
        .s(": ")
        .raw(&said)
        .s(".");
    if eq(kind, FIRST_SIGHT) && at.is_some() {
        line.s(" Until answered, nothing from other projects is shown ")
            .raw(&here)
            .s(" and what is written ")
            .raw(&here)
            .s(" stays with that folder.");
    } else if eq(kind, FIRST_SIGHT) {
        line.s(
            " Until answered, nothing from other projects is shown here and what is \
             written here stays with this folder.",
        );
    }
    line.s(" ").raw(&options).done()
}

// ---------------------------------------------------------------------------
// A stored row, as far as the restated question reads it
// ---------------------------------------------------------------------------

/// What the reference raises reading a row: the exception's class and
/// `str()` of it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Raised {
    pub class: &'static str,
    pub message: String,
}

fn raised(class: &'static str, message: impl Into<String>) -> Raised {
    Raised {
        class,
        message: message.into(),
    }
}

/// The fields `line_restated` takes from a question's row, read as
/// `membership_rows._proposal_from` (196-214) reads the row and
/// `membership_queries._restatement` the folder: `id`, `kind` and `reason`
/// through `str()`, `candidates` each through `str()`, `folder` the
/// evidence's `dir` where the kind is a part's question and it is a
/// non-empty str. `checkout` is the row's value as it is (the caller's
/// store read of `filed` takes it).
#[derive(Clone, Debug, PartialEq)]
pub struct RestatedRow {
    pub id: PyStr,
    pub checkout: PyValue,
    pub kind: PyStr,
    pub candidates: Vec<PyStr>,
    pub reason: PyStr,
    pub folder: Option<PyStr>,
}

fn type_name(value: &PyValue) -> &'static str {
    match value {
        PyValue::None => "NoneType",
        PyValue::Bool(_) => "bool",
        PyValue::Int(_) => "int",
        PyValue::Float(_) => "float",
        PyValue::Str(_) => "str",
        PyValue::DateTime(_) => "datetime.datetime",
        PyValue::Path(_) => "PurePosixPath",
        PyValue::Tuple(_) => "tuple",
        PyValue::List(_) => "list",
        PyValue::Dict(_) => "dict",
    }
}

fn truth(value: &PyValue) -> bool {
    match value {
        PyValue::None => false,
        PyValue::Bool(b) => *b,
        PyValue::Int(t) => t != "0",
        PyValue::Float(f) => *f != 0.0,
        PyValue::Str(s) | PyValue::Path(s) => !s.is_empty(),
        PyValue::DateTime(_) => true,
        PyValue::Tuple(v) | PyValue::List(v) => !v.is_empty(),
        PyValue::Dict(v) => !v.is_empty(),
    }
}

/// `iter(value)`'s items: a sequence's items, a str's characters, a dict's
/// keys; TypeError for anything else.
fn items(value: &PyValue) -> Result<Vec<PyValue>, Raised> {
    match value {
        PyValue::List(v) | PyValue::Tuple(v) => Ok(v.clone()),
        PyValue::Str(s) => Ok(s.iter().map(|&c| PyValue::Str(vec![c])).collect()),
        PyValue::Dict(v) => Ok(v.iter().map(|(k, _)| k.clone()).collect()),
        other => Err(raised(
            "TypeError",
            format!("'{}' object is not iterable", type_name(other)),
        )),
    }
}

/// `str(value)`, with the port's depth limit (`capture::str_of`).
fn str_raising(value: &PyValue) -> Result<PyStr, Raised> {
    str_of(value, STR_DEPTH_TOP).map_err(|e| match e {
        PyError::RecursionError => raised(
            "RecursionError",
            "maximum recursion depth exceeded while getting the repr of an object",
        ),
        PyError::ValueError => raised(
            "ValueError",
            "Exceeds the limit (4300 digits) for integer string conversion; use \
             sys.set_int_max_str_digits() to increase the limit",
        ),
        other => raised(other.name(), ""),
    })
}

/// The hash check a dict makes of a key: a list or a dict (also inside a
/// tuple) is unhashable.
fn hashable(key: &PyValue) -> Result<(), Raised> {
    let mut work = vec![key];
    while let Some(v) = work.pop() {
        match v {
            PyValue::List(_) | PyValue::Dict(_) => {
                return Err(raised(
                    "TypeError",
                    format!("unhashable type: '{}'", type_name(v)),
                ))
            }
            PyValue::Tuple(items) => work.extend(items.iter().rev()),
            _ => {}
        }
    }
    Ok(())
}

/// `dict(value or {})` reduced to what is read of it: the value of key
/// `"dir"`, the last one written. Raises as `dict()` does.
fn evidence_dir(value: &PyValue) -> Result<Option<PyValue>, Raised> {
    if !truth(value) {
        return Ok(None);
    }
    let pairs: Vec<(PyValue, PyValue)> = match value {
        PyValue::Dict(v) => v.clone(),
        PyValue::List(_) | PyValue::Tuple(_) | PyValue::Str(_) => {
            let mut pairs = Vec::new();
            for (i, element) in items(value)?.into_iter().enumerate() {
                let pair = match &element {
                    PyValue::List(_) | PyValue::Tuple(_) | PyValue::Str(_) | PyValue::Dict(_) => {
                        items(&element)?
                    }
                    _ => {
                        return Err(raised(
                            "TypeError",
                            format!(
                            "cannot convert dictionary update sequence element #{i} to a sequence"
                        ),
                        ))
                    }
                };
                if pair.len() != 2 {
                    return Err(raised(
                        "ValueError",
                        format!(
                            "dictionary update sequence element #{i} has length {}; 2 is required",
                            pair.len()
                        ),
                    ));
                }
                let mut pair = pair.into_iter();
                let (k, v) = (pair.next().expect("two"), pair.next().expect("two"));
                hashable(&k)?;
                pairs.push((k, v));
            }
            pairs
        }
        other => {
            return Err(raised(
                "TypeError",
                format!("'{}' object is not iterable", type_name(other)),
            ))
        }
    };
    let dir = cps("dir");
    Ok(pairs.into_iter().rev().find_map(|(k, v)| match &k {
        PyValue::Str(s) if *s == dir => Some(v),
        _ => None,
    }))
}

/// A question's row (a dict of its columns, `_json` ones decoded) read
/// as `_proposal_from` reads it, every column in its order, so the first
/// exception it raises is the one returned; then the folder
/// `_restatement` passes.
pub fn restated_row(row: &PyValue) -> Result<RestatedRow, Raised> {
    let get = |key: &str| {
        row.get(key)
            .ok_or_else(|| raised("KeyError", format!("'{key}'")))
    };
    let signature = get("signature_json")?;
    let id = str_raising(get("id")?)?;
    let checkout = get("checkout")?.clone();
    let kind = str_raising(get("kind")?)?;
    let stored = get("candidates_json")?;
    let mut candidates = Vec::new();
    if truth(stored) {
        for c in items(stored)? {
            candidates.push(str_raising(&c)?);
        }
    }
    let dir = evidence_dir(get("evidence_json")?)?;
    let reason = str_raising(get("reason")?)?;
    str_raising(get("line")?)?;
    str_raising(get("state")?)?;
    get("answer")?;
    get("answered_by")?;
    if !matches!(signature, PyValue::None) {
        items(signature)?;
    }
    str_raising(get("opened")?)?;
    let folder = match &dir {
        Some(PyValue::Str(s)) if !s.is_empty() && eq(&kind, SUBPROJECT_CONTRADICTION) => {
            Some(s.clone())
        }
        _ => None,
    };
    Ok(RestatedRow {
        id,
        checkout,
        kind,
        candidates,
        reason,
        folder,
    })
}
