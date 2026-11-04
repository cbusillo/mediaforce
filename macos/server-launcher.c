#include <Python.h>
#include <mach-o/dyld.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void check_status(PyStatus status, PyConfig *config) {
    if (PyStatus_Exception(status)) {
        PyConfig_Clear(config);
        Py_ExitStatusException(status);
    }
}

static void append_path(PyConfig *config, const char *contents, const char *suffix) {
    char path[PATH_MAX];
    if (snprintf(path, sizeof(path), "%s/%s", contents, suffix) >= sizeof(path)) {
        fprintf(stderr, "Mediaforce bundle path is too long\n");
        exit(1);
    }
    wchar_t *wide = Py_DecodeLocale(path, NULL);
    if (wide == NULL) exit(1);
    PyStatus status = PyWideStringList_Append(&config->module_search_paths, wide);
    PyMem_RawFree(wide);
    check_status(status, config);
}

int main(int argc, char **argv) {
    char executable[PATH_MAX], resolved[PATH_MAX], contents[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0 || realpath(executable, resolved) == NULL) {
        perror("Cannot locate Mediaforce server");
        return 1;
    }
    strlcpy(contents, resolved, sizeof(contents));
    for (int i = 0; i < 2; i++) {
        char *slash = strrchr(contents, '/');
        if (slash == NULL) return 1;
        *slash = '\0';
    }
    int python_mode = argc > 1 && (
        strcmp(argv[1], "-I") == 0 || strcmp(argv[1], "-S") == 0 ||
        strcmp(argv[1], "-c") == 0 || strcmp(argv[1], "-m") == 0
    );
    char **arguments = calloc((size_t)argc + 3, sizeof(char *));
    if (arguments == NULL) return 1;
    arguments[0] = resolved;
    int offset = python_mode ? 0 : 2;
    if (!python_mode) {
        arguments[1] = "-m";
        arguments[2] = "mediaforce.ops.bundled_server";
    }
    for (int i = 1; i < argc; i++) arguments[i + offset] = argv[i];
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.write_bytecode = 0;
    config.user_site_directory = 0;
    config.site_import = 0;
    check_status(PyConfig_SetBytesString(&config, &config.executable, resolved), &config);
    check_status(PyConfig_SetBytesArgv(&config, argc + offset, arguments), &config);
    check_status(PyConfig_Read(&config), &config);
    config.module_search_paths_set = 1;
    append_path(&config, contents, "Frameworks/Python.framework/Versions/" PYTHON_ABI "/lib/python" PYTHON_ABI);
    append_path(&config, contents, "Frameworks/Python.framework/Versions/" PYTHON_ABI "/lib/python" PYTHON_ABI "/lib-dynload");
    append_path(&config, contents, "Resources/server/app");
#if defined(__arm64__)
    append_path(&config, contents, "Resources/server/arm64");
#elif defined(__x86_64__)
    append_path(&config, contents, "Resources/server/x86_64");
#else
#error Unsupported Mediaforce architecture
#endif
    check_status(Py_InitializeFromConfig(&config), &config);
    PyConfig_Clear(&config);
    free(arguments);
    return Py_RunMain();
}
