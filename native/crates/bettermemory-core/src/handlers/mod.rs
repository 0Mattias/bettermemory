//! The MCP tool handlers of `src/bettermemory/handlers`, ported a gate at a
//! time. Each handler's effects (the store, the file system, the checks
//! the handler calls out to) reach it as arguments.

pub mod search_request;
pub mod verify;
