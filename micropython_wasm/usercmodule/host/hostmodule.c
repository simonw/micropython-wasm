#include <stddef.h>
#include <stdint.h>

#include "py/objstr.h"
#include "py/runtime.h"

#define HOST_RESULT_CAP (64 * 1024)

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
    char *result = m_new(char, HOST_RESULT_CAP);

    int32_t result_len = micropython_wasm_host_call(
        name,
        name_len,
        payload,
        payload_len,
        result,
        HOST_RESULT_CAP
        );

    if (result_len < 0) {
        m_del(char, result, HOST_RESULT_CAP);
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("host callback failed"));
    }
    if ((size_t)result_len > HOST_RESULT_CAP) {
        m_del(char, result, HOST_RESULT_CAP);
        mp_raise_msg(&mp_type_ValueError, MP_ERROR_TEXT("host callback result too large"));
    }

    mp_obj_t out = mp_obj_new_str(result, (size_t)result_len);
    m_del(char, result, HOST_RESULT_CAP);
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
