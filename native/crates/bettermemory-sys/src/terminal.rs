/// The width of the terminal attached to stdout (`os.get_terminal_size`).
/// A redirected stream or an unset window size has no usable width.
#[cfg(unix)]
pub fn stdout_columns() -> Option<u16> {
    let mut size = std::mem::MaybeUninit::<libc::winsize>::uninit();
    // SAFETY: TIOCGWINSZ writes one complete winsize at this valid address
    // on success. A closed or redirected fd simply fails the ioctl.
    if unsafe { libc::ioctl(libc::STDOUT_FILENO, libc::TIOCGWINSZ, size.as_mut_ptr()) } != 0 {
        return None;
    }
    // SAFETY: the ioctl above initialized the struct.
    let columns = unsafe { size.assume_init() }.ws_col;
    (columns > 0).then_some(columns)
}

#[cfg(windows)]
#[repr(C)]
#[derive(Default)]
struct ConsoleScreenBufferInfo {
    size: [i16; 2],
    cursor_position: [i16; 2],
    attributes: u16,
    window: [i16; 4],
    maximum_window_size: [i16; 2],
}

#[cfg(windows)]
#[link(name = "kernel32")]
extern "system" {
    fn GetConsoleScreenBufferInfo(
        output: *mut core::ffi::c_void,
        info: *mut ConsoleScreenBufferInfo,
    ) -> i32;
}

/// The visible window width of stdout's console, or None for a pipe.
#[cfg(windows)]
pub fn stdout_columns() -> Option<u16> {
    use std::os::windows::io::AsRawHandle;
    let stdout = std::io::stdout();
    let mut info = ConsoleScreenBufferInfo::default();
    // SAFETY: stdout owns the handle for this call, and info has the C
    // layout of CONSOLE_SCREEN_BUFFER_INFO and is writable in full.
    let ok = unsafe { GetConsoleScreenBufferInfo(stdout.as_raw_handle(), &mut info) };
    if ok == 0 {
        return None;
    }
    let [left, _, right, _] = info.window;
    u16::try_from(i32::from(right) - i32::from(left) + 1)
        .ok()
        .filter(|&width| width > 0)
}

/// On a platform without a terminal-size provider the caller uses its fallback.
#[cfg(not(any(unix, windows)))]
pub fn stdout_columns() -> Option<u16> {
    None
}
