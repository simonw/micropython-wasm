#include <stddef.h>
#include <stdint.h>

#include "py/mpstate.h"
#include "py/objstr.h"
#include "py/runtime.h"

#define DEFAULT_HOST_RESULT_CAP (256 * 1024)

__attribute__((import_module("micropython_wasm"), import_name("host_result_cap")))
extern int32_t micropython_wasm_host_result_cap(void);

__attribute__((import_module("micropython_wasm"), import_name("host_call")))
extern int32_t micropython_wasm_host_call(
    const char *name,
    size_t name_len,
    const char *payload,
    size_t payload_len,
    char *result,
    size_t result_cap
    );

static mp_obj_t host_call(mp_obj_t name_obj, mp_obj_t payload_obj) {
    size_t name_len;
    size_t payload_len;
    const char *name = mp_obj_str_get_data(name_obj, &name_len);
    const char *payload = mp_obj_str_get_data(payload_obj, &payload_len);
    int32_t result_cap = micropython_wasm_host_result_cap();
    if (result_cap <= 0) {
        result_cap = DEFAULT_HOST_RESULT_CAP;
    }
    char *result = MP_STATE_VM(host_result_buffer);
    if ((size_t)result_cap > MP_STATE_VM(host_result_buffer_cap)) {
        result = m_renew(
            char,
            result,
            MP_STATE_VM(host_result_buffer_cap),
            (size_t)result_cap
            );
        MP_STATE_VM(host_result_buffer) = result;
        MP_STATE_VM(host_result_buffer_cap) = (size_t)result_cap;
    }

    int32_t result_len = micropython_wasm_host_call(
        name,
        name_len,
        payload,
        payload_len,
        result,
        (size_t)result_cap
        );

    if (result_len < 0) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("host callback failed"));
    }
    if ((size_t)result_len > (size_t)result_cap) {
        mp_raise_msg(&mp_type_ValueError, MP_ERROR_TEXT("host callback result too large"));
    }

    mp_obj_t out = mp_obj_new_str(result, (size_t)result_len);
    return out;
}
static MP_DEFINE_CONST_FUN_OBJ_2(host_call_obj, host_call);

static const mp_rom_map_elem_t host_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_host) },
    { MP_ROM_QSTR(MP_QSTR_call), MP_ROM_PTR(&host_call_obj) },
};
static MP_DEFINE_CONST_DICT(host_module_globals, host_module_globals_table);

const mp_obj_module_t host_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&host_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_host, host_user_cmodule);
MP_REGISTER_ROOT_POINTER(char *host_result_buffer);
MP_REGISTER_ROOT_POINTER(size_t host_result_buffer_cap);
