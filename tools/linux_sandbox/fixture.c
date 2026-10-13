/* Trusted, disposable composition fixture. Never a Plugin launcher.
 * Build twice: -DPAYLOAD -static for the sandbox; otherwise link established
 * libsystemd/libseccomp for D-Bus FD handoff and filter export.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

static void fail(const char *what) {
#ifdef PAYLOAD
    int saved = errno;
    dprintf(4, "{\"event\":\"failure\",\"reason\":\"fixture check failed: %s (errno=%d)\"}\n", what, saved);
    errno = saved;
#endif
    fprintf(stderr, "composition fixture: %s: %s\n", what, strerror(errno));
    exit(1);
}

#ifdef PAYLOAD
#include <dirent.h>
#include <sys/mount.h>
#include <sys/statvfs.h>

static void require(bool value, const char *what) {
    if (!value) {
        errno = EINVAL;
        fail(what);
    }
}

static void check_readonly(const char *path) {
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    require(fd < 0 && errno == EROFS, path);
}

static void check_mounts(void) {
    FILE *input = fopen("/proc/self/mountinfo", "r");
    if (!input) fail("mountinfo");
    char *line = NULL;
    size_t capacity = 0;
    unsigned writable = 0;
    while (getline(&line, &capacity, input) >= 0) {
        char root[4096], target[4096], flags[256];
        require(sscanf(line, "%*u %*u %*s %4095s %4095s %255s",
                       root, target, flags) == 3, "mountinfo syntax");
        if (!strncmp(flags, "rw,", 3) || !strcmp(flags, "rw")) {
            require(!strcmp(target, "/scratch"), "unexpected writable mount");
            writable++;
        }
    }
    free(line);
    fclose(input);
    require(writable == 1, "exactly one writable mount");
    check_readonly("/root-write");
    check_readonly("/dev/dev-write");
    int executable = open("/fixture", O_WRONLY);
    require(executable < 0 && errno == EROFS, "fixture is read-only");
    struct stat a, b, c;
    require(!stat("/scratch", &a) && !stat("/tmp", &b) &&
            !stat("/dev/shm", &c), "scratch aliases");
    require(a.st_dev == b.st_dev && a.st_ino == b.st_ino &&
            a.st_dev == c.st_dev && a.st_ino == c.st_ino,
            "scratch aliases must share the same quota");
}

static void check_descriptors(void) {
    struct stat st;
    require(!fstat(3, &st) && S_ISFIFO(st.st_mode), "broker request pipe");
    require(!fstat(4, &st) && S_ISFIFO(st.st_mode), "broker reply pipe");
    DIR *directory = opendir("/proc/self/fd");
    if (!directory) fail("descriptor inventory");
    struct dirent *entry;
    while ((entry = readdir(directory))) {
        char *end;
        long fd = strtol(entry->d_name, &end, 10);
        if (!*end) require(fd <= 4 || fd == dirfd(directory), "unexpected inherited fd");
    }
    closedir(directory);
}

static void check_filter(void) {
    require(prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) == 1, "no_new_privs");
    require(prctl(PR_GET_SECCOMP, 0, 0, 0, 0) == 2, "seccomp filter loaded");
    require(socket(AF_UNIX, SOCK_STREAM, 0) < 0 && errno == EPERM, "Unix socket blocked");
    require(socket(AF_INET, SOCK_STREAM, 0) < 0 && errno == EPERM, "network socket blocked");
    require(unshare(CLONE_NEWUSER) < 0 && errno == EPERM, "nested namespace blocked");
    require(mount("tmpfs", "/scratch", "tmpfs", 0, NULL) < 0 && errno == EPERM,
            "mount blocked");
    FILE *status = fopen("/proc/self/status", "r");
    if (!status) fail("process status");
    char line[512];
    bool caps = false;
    while (fgets(line, sizeof(line), status)) {
        unsigned long long value;
        if (sscanf(line, "CapEff: %llx", &value) == 1) {
            require(value == 0, "capabilities dropped");
            caps = true;
        }
    }
    fclose(status);
    require(caps, "capability evidence missing");
}

static unsigned long check_bytes(void) {
    int fd = open("/scratch/bytes", O_CREAT | O_EXCL | O_WRONLY, 0600);
    if (fd < 0) fail("byte quota fixture");
    char block[4096] = {0};
    unsigned long total = 0;
    for (;;) {
        ssize_t size = write(fd, block, sizeof(block));
        if (size < 0) {
            require(errno == ENOSPC, "byte quota must return ENOSPC");
            break;
        }
        require(size > 0, "byte quota made no progress");
        total += (unsigned long)size;
        require(total <= 1048576, "byte quota exceeded 1 MiB");
    }
    require(total > 0, "byte quota had no capacity");
    close(fd);
    require(!unlink("/scratch/bytes"), "remove byte fixture");
    return total;
}

static unsigned check_inodes(void) {
    unsigned count = 0;
    char name[64];
    for (; count < 128; count++) {
        snprintf(name, sizeof(name), "/scratch/inode-%u", count);
        int fd = open(name, O_CREAT | O_EXCL | O_WRONLY, 0600);
        if (fd < 0) {
            require(errno == ENOSPC, "inode quota must return ENOSPC");
            break;
        }
        close(fd);
    }
    require(count > 0 && count < 64, "inode quota exceeded configured limit");
    for (unsigned i = 0; i < count; i++) {
        snprintf(name, sizeof(name), "/scratch/inode-%u", i);
        require(!unlink(name), "remove inode fixture");
    }
    return count;
}

int main(int argc, char **argv) {
    require(argc == 2 && strlen(argv[1]) == 32, "internal token");
    for (const char *p = argv[1]; *p; p++)
        require((*p >= 'a' && *p <= 'f') || (*p >= '0' && *p <= '9'), "token alphabet");
    check_mounts();
    check_descriptors();
    check_filter();
    struct statvfs quota;
    require(!statvfs("/scratch", &quota), "scratch statvfs");
    require(quota.f_blocks * quota.f_frsize == 1048576 && quota.f_files == 64,
            "effective tmpfs byte/inode limits");
    unsigned long bytes = check_bytes();
    unsigned inodes = check_inodes();
    FILE *marker = fopen("/scratch/owner", "wx");
    if (!marker) fail("private marker");
    require(fputs(argv[1], marker) >= 0 && !fclose(marker), "write private marker");
    FILE *requests = fdopen(3, "r"), *replies = fdopen(4, "w");
    require(requests && replies, "broker streams");
    setvbuf(replies, NULL, _IOLBF, 0);
    signal(SIGTERM, SIG_IGN);
    pid_t descendant = fork();
    if (descendant < 0) fail("descendant fixture");
    if (!descendant) {
        close(3);
        close(4);
        for (;;) pause();
    }
    fprintf(replies, "{\"event\":\"fixture_ready\",\"token\":\"%s\","
            "\"bytes_before_enospc\":%lu,\"files_before_enospc\":%u,"
            "\"writable_mounts\":[\"/scratch\"],\"scratch_aliases\":[\"/tmp\",\"/dev/shm\"],"
            "\"seccomp\":true,\"no_new_privs\":true,\"zero_capabilities\":true,"
            "\"descendant_namespace_pid\":%ld}\n", argv[1], bytes, inodes, (long)descendant);
    char challenge[128];
    while (fgets(challenge, sizeof(challenge), requests)) {
        challenge[strcspn(challenge, "\n")] = 0;
        require(!strcmp(challenge, argv[1]), "broker crossed Host boundary");
        marker = fopen("/scratch/owner", "r");
        char owner[64] = {0};
        require(marker && fgets(owner, sizeof(owner), marker), "read private marker");
        fclose(marker);
        require(!strcmp(owner, argv[1]), "scratch crossed Host boundary");
        fprintf(replies, "{\"event\":\"isolated\",\"token\":\"%s\"}\n", argv[1]);
    }
    return 0;
}

#else
#include <seccomp.h>
#include <systemd/sd-bus.h>

static void bus_ok(int result) {
    if (result < 0) {
        errno = -result;
        fail("systemd D-Bus");
    }
}

static void string_property(sd_bus_message *message, const char *name, const char *value) {
    bus_ok(sd_bus_message_open_container(message, 'r', "sv"));
    bus_ok(sd_bus_message_append(message, "s", name));
    bus_ok(sd_bus_message_open_container(message, 'v', "s"));
    bus_ok(sd_bus_message_append(message, "s", value));
    bus_ok(sd_bus_message_close_container(message));
    bus_ok(sd_bus_message_close_container(message));
}

static int start_service(char **argv) {
    sd_bus *bus = NULL;
    sd_bus_message *message = NULL, *reply = NULL;
    sd_bus_error error = SD_BUS_ERROR_NULL;
    bus_ok(sd_bus_open_user(&bus));
    bus_ok(sd_bus_message_new_method_call(bus, &message, "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "StartTransientUnit"));
    bus_ok(sd_bus_message_append(message, "ss", argv[2], "fail"));
    bus_ok(sd_bus_message_open_container(message, 'a', "(sv)"));
    string_property(message, "Description", "Penhin disposable composition proof (trusted fixture only)");
    string_property(message, "Type", "exec");
    string_property(message, "ExitType", "main");
    string_property(message, "KillMode", "control-group");
    string_property(message, "Restart", "no");
    string_property(message, "DelegateSubgroup", "supervisor");
    bus_ok(sd_bus_message_append(message, "(sv)", "Delegate", "b", 1));
    bus_ok(sd_bus_message_append(message, "(sv)", "TimeoutStopUSec", "t", (uint64_t)1000000));
    bus_ok(sd_bus_message_append(message, "(sv)", "RuntimeMaxUSec", "t", (uint64_t)120000000));
    bus_ok(sd_bus_message_open_container(message, 'r', "sv"));
    bus_ok(sd_bus_message_append(message, "s", "ExecStart"));
    bus_ok(sd_bus_message_open_container(message, 'v', "a(sasb)"));
    bus_ok(sd_bus_message_open_container(message, 'a', "(sasb)"));
    bus_ok(sd_bus_message_open_container(message, 'r', "sasb"));
    bus_ok(sd_bus_message_append(message, "s", argv[3]));
    char *command[] = {argv[3], "-I", "-S", argv[4], "_supervisor", argv[5], argv[6], NULL};
    bus_ok(sd_bus_message_append_strv(message, command));
    bus_ok(sd_bus_message_append(message, "b", 0));
    bus_ok(sd_bus_message_close_container(message));
    bus_ok(sd_bus_message_close_container(message));
    bus_ok(sd_bus_message_close_container(message));
    bus_ok(sd_bus_message_close_container(message));
    bus_ok(sd_bus_message_open_container(message, 'r', "sv"));
    bus_ok(sd_bus_message_append(message, "s", "ExtraFileDescriptors"));
    bus_ok(sd_bus_message_open_container(message, 'v', "a(hs)"));
    bus_ok(sd_bus_message_open_container(message, 'a', "(hs)"));
    const char *names[] = {"broker-request", "broker-reply", "lease", "evidence"};
    for (int i = 0; i < 4; i++) {
        char *end;
        long fd = strtol(argv[7 + i], &end, 10);
        if (*end || fd < 3 || fd > 1048576) { errno = EINVAL; fail("internal fd"); }
        bus_ok(sd_bus_message_append(message, "(hs)", (int)fd, names[i]));
    }
    bus_ok(sd_bus_message_close_container(message));
    bus_ok(sd_bus_message_close_container(message));
    bus_ok(sd_bus_message_close_container(message));
    bus_ok(sd_bus_message_close_container(message));
    bus_ok(sd_bus_message_append(message, "a(sa(sv))", 0));
    int result = sd_bus_call(bus, message, 15000000, &error, &reply);
    if (result < 0) {
        fprintf(stderr, "StartTransientUnit: %s\n", error.message ? error.message : "failed");
        bus_ok(result);
    }
    sd_bus_error_free(&error);
    sd_bus_message_unref(reply);
    sd_bus_message_unref(message);
    sd_bus_unref(bus);
    return 0;
}

static int export_filter(const char *path) {
    /* Deliberately fixture-specific prohibitions, NOT a production Python policy.
     * libseccomp performs all architecture-aware BPF compilation.
     */
    scmp_filter_ctx filter = seccomp_init(SCMP_ACT_ALLOW);
    if (!filter) fail("seccomp_init");
    const char *denied[] = {"socket", "socketpair", "connect", "bind", "listen", "accept",
        "accept4", "unshare", "setns", "mount", "umount2", "pivot_root", "chroot", "ptrace",
        "bpf", "keyctl", "perf_event_open", "open_by_handle_at", "userfaultfd", "io_uring_setup"};
    for (size_t i = 0; i < sizeof(denied) / sizeof(denied[0]); i++) {
        int syscall = seccomp_syscall_resolve_name(denied[i]);
        if (syscall == __NR_SCMP_ERROR) { errno = ENOSYS; fail(denied[i]); }
        bus_ok(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), syscall, 0));
    }
    bus_ok(seccomp_rule_add(filter, SCMP_ACT_ERRNO(ENOSYS), SCMP_SYS(clone3), 0));
    const unsigned long namespaces[] = {CLONE_NEWUSER, CLONE_NEWNS, CLONE_NEWPID,
        CLONE_NEWNET, CLONE_NEWIPC, CLONE_NEWUTS, CLONE_NEWCGROUP};
    for (size_t i = 0; i < sizeof(namespaces) / sizeof(namespaces[0]); i++)
        bus_ok(seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(clone), 1,
               SCMP_A0(SCMP_CMP_MASKED_EQ, namespaces[i], namespaces[i])));
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (fd < 0) fail("filter output");
    bus_ok(seccomp_export_bpf(filter, fd));
    if (close(fd)) fail("filter close");
    seccomp_release(filter);
    return 0;
}

int main(int argc, char **argv) {
    if (argc == 11 && !strcmp(argv[1], "--start")) return start_service(argv);
    if (argc == 3 && !strcmp(argv[1], "--filter")) return export_filter(argv[2]);
    fprintf(stderr, "Internal helper for composition.py; no arbitrary payload interface.\n");
    return 2;
}
#endif
