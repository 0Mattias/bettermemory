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

use crate::printable::{printable_into, quoted_into};
use crate::pyvalue::PyStr;

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
