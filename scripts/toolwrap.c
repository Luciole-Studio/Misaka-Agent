#define _POSIX_C_SOURCE 200809L
#define _XOPEN_SOURCE 700

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <limits.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif
#endif

#define CAP_ARGS 256
#define CAP_ENV 64
#define CAP_PREPEND 8
#define WRAP_LINE 4096

typedef struct Spec {
    char *exec_path;
    char *args[CAP_ARGS];
    int nargs;
    char *env[CAP_ENV];
    int nenv;
    char *prepend[CAP_PREPEND];
    int nprepend;
} Spec;

static void fail(const char *message) {
    fprintf(stderr, "misaka: %s\n", message);
    exit(127);
}

static void fail_errno(const char *message) {
    fprintf(stderr, "misaka: %s: %s\n", message, strerror(errno));
    exit(127);
}

static char *duplicate(const char *text) {
    size_t length = strlen(text) + 1;
    char *copy = (char *)malloc(length);
    if (!copy) {
        fail("out of memory");
    }
    memcpy(copy, text, length);
    return copy;
}

static void strip_line(char *line) {
    size_t length = strlen(line);
    while (length > 0 && (line[length - 1] == '\n' || line[length - 1] == '\r')) {
        line[--length] = '\0';
    }
}

static void load_spec(FILE *file, const char *path, Spec *spec) {
    char line[WRAP_LINE];
    memset(spec, 0, sizeof(*spec));
    if (!file) {
        fail_errno(path);
    }
    while (fgets(line, sizeof line, file)) {
        char *value;
        strip_line(line);
        if (line[0] == '\0' || line[0] == '#') {
            continue;
        }
        value = strchr(line, '=');
        if (!value) {
            fprintf(stderr, "misaka: bad wrap line in %s: %s\n", path, line);
            exit(127);
        }
        *value = '\0';
        value += 1;
        if (strcmp(line, "exec") == 0) {
            spec->exec_path = duplicate(value);
        } else if (strcmp(line, "arg") == 0) {
            if (spec->nargs >= CAP_ARGS) {
                fail("too many arg lines in the wrap file");
            }
            spec->args[spec->nargs++] = duplicate(value);
        } else if (strcmp(line, "env") == 0) {
            if (spec->nenv >= CAP_ENV) {
                fail("too many env lines in the wrap file");
            }
            if (!strchr(value, '=')) {
                fail("env lines look like env=NAME=value");
            }
            spec->env[spec->nenv++] = duplicate(value);
        } else if (strcmp(line, "path_prepend") == 0) {
            if (spec->nprepend >= CAP_PREPEND) {
                fail("too many path_prepend lines in the wrap file");
            }
            spec->prepend[spec->nprepend++] = duplicate(value);
        } else {
            fprintf(stderr, "misaka: unknown wrap directive '%s' in %s\n", line, path);
            exit(127);
        }
    }
    fclose(file);
    if (!spec->exec_path || spec->exec_path[0] == '\0') {
        fail("the wrap file has no exec= line");
    }
}

static int is_relative(const char *path) {
    if (path[0] == '/' || path[0] == '\\') {
        return 0;
    }
    if (((path[0] >= 'A' && path[0] <= 'Z') || (path[0] >= 'a' && path[0] <= 'z')) && path[1] == ':') {
        return 0;
    }
    return 1;
}

#ifdef _WIN32

static wchar_t *to_wide(const char *text) {
    int count = MultiByteToWideChar(CP_UTF8, 0, text, -1, NULL, 0);
    wchar_t *wide;
    if (count <= 0) {
        fail("a wrap path is not valid UTF-8");
    }
    wide = (wchar_t *)malloc((size_t)count * sizeof(wchar_t));
    if (!wide) {
        fail("out of memory");
    }
    MultiByteToWideChar(CP_UTF8, 0, text, -1, wide, count);
    return wide;
}

static void to_backslash(wchar_t *text) {
    for (; *text; text++) {
        if (*text == L'/') {
            *text = L'\\';
        }
    }
}

static wchar_t *absolute_path(const wchar_t *exe_dir, const char *value) {
    wchar_t *rel = to_wide(value);
    wchar_t joined[32768];
    wchar_t full[32768];
    to_backslash(rel);
    if (!is_relative(value)) {
        wcsncpy(joined, rel, 32767);
        joined[32767] = L'\0';
    } else if (_snwprintf(joined, 32768, L"%s\\%s", exe_dir, rel) < 0) {
        fail("a wrap path is too long");
    }
    free(rel);
    if (!_wfullpath(full, joined, 32768)) {
        fail("could not resolve a wrap path");
    }
    return _wcsdup(full);
}

static wchar_t *executable_path(void) {
    wchar_t buffer[32768];
    DWORD length = GetModuleFileNameW(NULL, buffer, 32768);
    if (length == 0 || length >= 32767) {
        fail("GetModuleFileName failed");
    }
    return _wcsdup(buffer);
}

static void strip_filename_w(wchar_t *path) {
    wchar_t *slash = wcsrchr(path, L'\\');
    if (!slash) {
        fail("the launcher path has no directory");
    }
    if (slash == path) {
        slash[1] = L'\0';
        return;
    }
    *slash = L'\0';
}

static void stem_name(const wchar_t *exe, wchar_t *stem, size_t cap) {
    const wchar_t *base = wcsrchr(exe, L'\\');
    size_t length;
    base = base ? base + 1 : exe;
    wcsncpy(stem, base, cap - 1);
    stem[cap - 1] = L'\0';
    length = wcslen(stem);
    if (length > 4 && _wcsicmp(stem + length - 4, L".exe") == 0) {
        stem[length - 4] = L'\0';
    }
}

static void append_quoted(wchar_t *command, size_t cap, const wchar_t *arg) {
    const wchar_t *cursor;
    int quote = arg[0] == L'\0';
    int backslashes = 0;
    size_t used = wcslen(command);
    for (cursor = arg; *cursor; cursor++) {
        if (*cursor == L' ' || *cursor == L'\t' || *cursor == L'"') {
            quote = 1;
        }
    }
    if (used > 0 && used + 1 < cap) {
        command[used++] = L' ';
        command[used] = L'\0';
    }
    if (!quote) {
        if (used + wcslen(arg) + 1 >= cap) {
            fail("the command line is too long");
        }
        wcscat(command, arg);
        return;
    }
    if (used + 1 >= cap) {
        fail("the command line is too long");
    }
    command[used++] = L'"';
    command[used] = L'\0';
    for (cursor = arg; *cursor; cursor++) {
        int index;
        if (*cursor == L'\\') {
            backslashes++;
            continue;
        }
        if (*cursor == L'"') {
            backslashes = backslashes * 2 + 1;
        }
        if (used + (size_t)backslashes + 2 >= cap) {
            fail("the command line is too long");
        }
        for (index = 0; index < backslashes; index++) {
            command[used++] = L'\\';
        }
        command[used++] = *cursor;
        command[used] = L'\0';
        backslashes = 0;
    }
    if (used + (size_t)backslashes * 2 + 2 >= cap) {
        fail("the command line is too long");
    }
    for (backslashes *= 2; backslashes > 0; backslashes--) {
        command[used++] = L'\\';
    }
    command[used++] = L'"';
    command[used] = L'\0';
}

static void prepend_path(const wchar_t *directory) {
    wchar_t updated[32768];
    DWORD current = GetEnvironmentVariableW(L"PATH", updated, 32768);
    wchar_t next[32768];
    if (current == 0) {
        updated[0] = L'\0';
    } else if (current >= 32768) {
        fail("PATH is too long");
    }
    if (_snwprintf(next, 32768, L"%s;%s", directory, updated) < 0) {
        fail("PATH is too long");
    }
    if (!SetEnvironmentVariableW(L"PATH", next)) {
        fail("could not update PATH");
    }
}

int wmain(int argc, wchar_t **argv) {
    wchar_t *exe = executable_path();
    wchar_t *exe_dir = _wcsdup(exe);
    wchar_t stem[256];
    wchar_t wrap_path[32768];
    char wrap_utf8[32768];
    Spec spec;
    wchar_t *program;
    wchar_t *command;
    int index;
    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    DWORD status = 1;
    stem_name(exe, stem, 256);
    strip_filename_w(exe_dir);
    if (_snwprintf(wrap_path, 32768, L"%s\\%s.wrap", exe_dir, stem) < 0) {
        fail("the wrap path is too long");
    }
    if (WideCharToMultiByte(CP_UTF8, 0, wrap_path, -1, wrap_utf8, sizeof wrap_utf8, NULL, NULL) <= 0) {
        fail("could not encode the wrap path");
    }
    load_spec(_wfopen(wrap_path, L"rb"), wrap_utf8, &spec);
    program = absolute_path(exe_dir, spec.exec_path);
    for (index = 0; index < spec.nenv; index++) {
        char *name = spec.env[index];
        char *value = strchr(name, '=');
        wchar_t *wide_name;
        wchar_t *wide_value;
        *value = '\0';
        value += 1;
        wide_name = to_wide(name);
        if (is_relative(value) && (strchr(value, '/') || strchr(value, '\\'))) {
            wchar_t *full = absolute_path(exe_dir, value);
            SetEnvironmentVariableW(wide_name, full);
            free(full);
        } else {
            wide_value = to_wide(value);
            SetEnvironmentVariableW(wide_name, wide_value);
            free(wide_value);
        }
        free(wide_name);
    }
    for (index = spec.nprepend - 1; index >= 0; index--) {
        wchar_t *directory = absolute_path(exe_dir, spec.prepend[index]);
        prepend_path(directory);
        free(directory);
    }
    command = (wchar_t *)calloc(65536, sizeof(wchar_t));
    if (!command) {
        fail("out of memory");
    }
    append_quoted(command, 65536, program);
    for (index = 0; index < spec.nargs; index++) {
        wchar_t *arg = to_wide(spec.args[index]);
        append_quoted(command, 65536, arg);
        free(arg);
    }
    for (index = 1; index < argc; index++) {
        append_quoted(command, 65536, argv[index]);
    }
    memset(&startup, 0, sizeof startup);
    startup.cb = sizeof startup;
    memset(&process, 0, sizeof process);
    if (!CreateProcessW(program, command, NULL, NULL, TRUE, 0, NULL, NULL, &startup, &process)) {
        fprintf(stderr, "misaka: could not start the bundled program (%lu)\n", GetLastError());
        return 127;
    }
    WaitForSingleObject(process.hProcess, INFINITE);
    GetExitCodeProcess(process.hProcess, &status);
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return (int)status;
}

#else

static char *executable_path(void) {
#ifdef __APPLE__
    char raw[PATH_MAX];
    uint32_t size = (uint32_t)sizeof raw;
    char *resolved;
    if (_NSGetExecutablePath(raw, &size) != 0) {
        fail("the launcher path is too long");
    }
    resolved = realpath(raw, NULL);
    if (!resolved) {
        fail_errno(raw);
    }
    return resolved;
#else
    char raw[PATH_MAX];
    ssize_t length = readlink("/proc/self/exe", raw, sizeof raw - 1);
    if (length < 0) {
        fail_errno("/proc/self/exe");
    }
    if ((size_t)length >= sizeof raw - 1) {
        fail("the launcher path is too long");
    }
    raw[length] = '\0';
    return duplicate(raw);
#endif
}

static void strip_filename(char *path) {
    char *slash = strrchr(path, '/');
    if (!slash) {
        fail("the launcher path has no directory");
    }
    if (slash == path) {
        slash[1] = '\0';
        return;
    }
    *slash = '\0';
}

static char *absolute_path(const char *exe_dir, const char *value) {
    char joined[PATH_MAX];
    const char *path = value;
    char *resolved;
    if (is_relative(value)) {
        if (snprintf(joined, sizeof joined, "%s/%s", exe_dir, value) >= (int)sizeof joined) {
            fail("a wrap path is too long");
        }
        path = joined;
    }
    resolved = realpath(path, NULL);
    if (resolved) {
        return resolved;
    }
    return duplicate(path);
}

static void prepend_path(const char *directory) {
    const char *current = getenv("PATH");
    char *next;
    size_t length;
    if (!current) {
        current = "";
    }
    length = strlen(directory) + strlen(current) + 2;
    next = (char *)malloc(length);
    if (!next) {
        fail("out of memory");
    }
    snprintf(next, length, "%s:%s", directory, current);
    if (setenv("PATH", next, 1) != 0) {
        fail_errno("PATH");
    }
    free(next);
}

int main(int argc, char **argv) {
    char *exe = executable_path();
    char *exe_dir = duplicate(exe);
    const char *base = strrchr(exe, '/');
    char stem[256];
    char wrap_path[PATH_MAX];
    Spec spec;
    char *program;
    char **child;
    int count;
    int index;
    base = base ? base + 1 : exe;
    snprintf(stem, sizeof stem, "%s", base);
    strip_filename(exe_dir);
    if (snprintf(wrap_path, sizeof wrap_path, "%s/%s.wrap", exe_dir, stem) >= (int)sizeof wrap_path) {
        fail("the wrap path is too long");
    }
    load_spec(fopen(wrap_path, "rb"), wrap_path, &spec);
    program = absolute_path(exe_dir, spec.exec_path);
    for (index = 0; index < spec.nenv; index++) {
        char *name = spec.env[index];
        char *value = strchr(name, '=');
        char *full = NULL;
        *value = '\0';
        value += 1;
        if (is_relative(value) && (strchr(value, '/') || strchr(value, '\\'))) {
            full = absolute_path(exe_dir, value);
            value = full;
        }
        if (setenv(name, value, 1) != 0) {
            fail_errno(name);
        }
        free(full);
    }
    for (index = spec.nprepend - 1; index >= 0; index--) {
        char *directory = absolute_path(exe_dir, spec.prepend[index]);
        prepend_path(directory);
        free(directory);
    }
    count = 1 + spec.nargs + (argc - 1);
    child = (char **)calloc((size_t)count + 1, sizeof(char *));
    if (!child) {
        fail("out of memory");
    }
    child[0] = program;
    for (index = 0; index < spec.nargs; index++) {
        child[1 + index] = spec.args[index];
    }
    for (index = 1; index < argc; index++) {
        child[spec.nargs + index] = argv[index];
    }
    execv(program, child);
    fail_errno(program);
    return 127;
}

#endif
