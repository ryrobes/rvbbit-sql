use std::ffi::CStr;

use pgrx::pg_sys;

pub(crate) fn nonsystem_view_access_restricted() -> bool {
    let value = unsafe {
        let ptr = pg_sys::GetConfigOption(
            c"restrict_nonsystem_relation_kind".as_ptr(),
            true,
            false,
        );
        if ptr.is_null() {
            return false;
        }
        CStr::from_ptr(ptr).to_string_lossy().into_owned()
    };
    value
        .split(',')
        .any(|part| part.trim().eq_ignore_ascii_case("view"))
}
