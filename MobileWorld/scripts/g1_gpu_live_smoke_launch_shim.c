/*
 * D-034 pre-gate launch shim.
 *
 * This is a Linux x86_64 freestanding program.  It is deliberately built
 * without libc, a dynamic loader, constructors, writable/executable segments,
 * networking, process creation, or signal operations.  Its only successful
 * terminal operation is execveat(AT_EMPTY_PATH) of the authority-bound system
 * Python file descriptor with the frozen Stage0 bootstrap and closed argv/env.
 */

typedef unsigned char u8;
typedef unsigned int u32;
typedef unsigned long u64;
typedef unsigned long usize;
typedef long isize;

#define NULL ((void *)0)
#define AT_FDCWD (-100)
#define AT_EMPTY_PATH 0x1000
#define O_RDONLY 0
#define O_DIRECTORY 00200000
#define O_CLOEXEC 02000000
#define O_NOFOLLOW 00400000
#define S_IFMT 0170000
#define S_IFREG 0100000
#define S_IFDIR 0040000
#define S_IFSOCK 0140000
#define MODE_MASK 07777

#define SYS_READ 0
#define SYS_CLOSE 3
#define SYS_FSTAT 5
#define SYS_OPENAT 257
#define SYS_READLINKAT 267
#define SYS_EXECVEAT 322
#define SYS_CLOSE_RANGE 436
#define SYS_EXIT_GROUP 231

#define MAX_AUTHORITY_BYTES 131072
#define MAX_PATH_BYTES 4096
#define MAX_JSON_DEPTH 64
#define SHA256_HEX_BYTES 64

#ifndef D034_AUTHORITY_PATH
#define D034_AUTHORITY_PATH \
    "/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/" \
    "d034-9845577c/authority.v3.json"
#endif
#ifndef D034_SHIM_PATH
#define D034_SHIM_PATH \
    "/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/" \
    "d034-9845577c/launch-shim.v2"
#endif
#ifndef D034_OUTER_PYTHON_UID
#define D034_OUTER_PYTHON_UID 0
#endif
#ifndef D034_OUTER_PYTHON_GID
#define D034_OUTER_PYTHON_GID 0
#endif
static const char AUTHORITY_PATH[] = D034_AUTHORITY_PATH;
static const char SHIM_PATH[] = D034_SHIM_PATH;
static const char TOKEN_PREFIX[] = "D034_STAGE0_V2";
static const char AUTHORITY_SCHEMA[] = "mobileworld.g1.gpu-live-smoke-authority/v3";
static const char AUTHORIZED_SCOPE[] = "SYNTHETIC_NON_CASE_GPU_LIVE_SMOKE_22_CALLS";
static const char SHIM_SCHEMA[] = "mobileworld.g1.gpu-live-smoke-launch-shim/v2";
static const char CONFIRMATION[] = "EXECUTE-D034-SYNTHETIC-22-CALL-SMOKE";
static const char BOOTSTRAP_SHA256[] =
    "70ac78cc43407933ff72b43925c309823fc852e654367d8576fb74b18811e63b";

struct timespec64 {
    isize tv_sec;
    isize tv_nsec;
};

/* Linux x86_64 kernel stat layout. */
struct kernel_stat {
    u64 st_dev;
    u64 st_ino;
    u64 st_nlink;
    u32 st_mode;
    u32 st_uid;
    u32 st_gid;
    u32 pad0;
    u64 st_rdev;
    isize st_size;
    isize st_blksize;
    isize st_blocks;
    struct timespec64 st_atim;
    struct timespec64 st_mtim;
    struct timespec64 st_ctim;
    isize unused[3];
};

static inline isize syscall1(isize number, isize a1) {
    isize result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(a1) : "rcx", "r11", "memory");
    return result;
}

static inline isize syscall2(isize number, isize a1, isize a2) {
    isize result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(a1), "S"(a2) : "rcx", "r11", "memory");
    return result;
}

static inline isize syscall3(isize number, isize a1, isize a2, isize a3) {
    isize result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(a1), "S"(a2), "d"(a3) : "rcx", "r11", "memory");
    return result;
}

static inline isize syscall4(isize number, isize a1, isize a2, isize a3, isize a4) {
    register isize r10 __asm__("r10") = a4;
    isize result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(a1), "S"(a2), "d"(a3), "r"(r10) : "rcx", "r11", "memory");
    return result;
}

static inline isize syscall5(
    isize number, isize a1, isize a2, isize a3, isize a4, isize a5
) {
    register isize r10 __asm__("r10") = a4;
    register isize r8 __asm__("r8") = a5;
    isize result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(a1), "S"(a2), "d"(a3), "r"(r10), "r"(r8) : "rcx", "r11", "memory");
    return result;
}

__attribute__((noreturn)) static void fail(void) {
    syscall1(SYS_EXIT_GROUP, 125);
    __builtin_unreachable();
}

static usize string_length(const char *value) {
    usize length = 0;
    while (value[length] != '\0') {
        length++;
    }
    return length;
}

static int bounded_string_length(const char *value, usize limit, usize *observed) {
    usize length;
    if (value == NULL) {
        return 0;
    }
    for (length = 0; length <= limit; length++) {
        if (value[length] == '\0') {
            *observed = length;
            return 1;
        }
    }
    return 0;
}

static int bytes_equal(const char *left, const char *right, usize count) {
    usize index;
    for (index = 0; index < count; index++) {
        if ((u8)left[index] != (u8)right[index]) {
            return 0;
        }
    }
    return 1;
}

static int string_equal(const char *left, const char *right) {
    usize left_length = string_length(left);
    usize right_length = string_length(right);
    return left_length == right_length && bytes_equal(left, right, left_length);
}

static int string_starts_with(const char *value, const char *prefix) {
    usize index;
    for (index = 0; prefix[index] != '\0'; index++) {
        if (value[index] != prefix[index]) {
            return 0;
        }
    }
    return 1;
}

static int environment_name_equal(const char *entry, const char *name) {
    usize length = string_length(name);
    return string_starts_with(entry, name) &&
           (entry[length] == '\0' || entry[length] == '=');
}

static void bytes_copy(char *destination, const char *source, usize count) {
    usize index;
    for (index = 0; index < count; index++) {
        destination[index] = source[index];
    }
}

static int is_lower_hex(char value) {
    return (value >= '0' && value <= '9') || (value >= 'a' && value <= 'f');
}

static int safe_absolute_path(const char *path) {
    usize index = 1;
    usize length = string_length(path);
    if (length < 2 || length >= MAX_PATH_BYTES || path[0] != '/' || path[length - 1] == '/') {
        return 0;
    }
    while (index < length) {
        usize segment_start = index;
        while (index < length && path[index] != '/') {
            char value = path[index];
            if (!((value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z') ||
                  (value >= '0' && value <= '9') || value == '.' || value == '_' ||
                  value == '-')) {
                return 0;
            }
            index++;
        }
        if (index == segment_start ||
            (index - segment_start == 1 && path[segment_start] == '.') ||
            (index - segment_start == 2 && path[segment_start] == '.' &&
             path[segment_start + 1] == '.')) {
            return 0;
        }
        if (index < length) {
            index++;
        }
    }
    return 1;
}

static int metadata_equal(const struct kernel_stat *left, const struct kernel_stat *right) {
    return left->st_dev == right->st_dev && left->st_ino == right->st_ino &&
           left->st_nlink == right->st_nlink && left->st_mode == right->st_mode &&
           left->st_uid == right->st_uid && left->st_gid == right->st_gid &&
           left->st_size == right->st_size &&
           left->st_mtim.tv_sec == right->st_mtim.tv_sec &&
           left->st_mtim.tv_nsec == right->st_mtim.tv_nsec &&
           left->st_ctim.tv_sec == right->st_ctim.tv_sec &&
           left->st_ctim.tv_nsec == right->st_ctim.tv_nsec;
}

static isize open_absolute_nofollow(const char *path, int final_flags) {
    char component[256];
    usize index = 1;
    isize directory_fd;
    if (!safe_absolute_path(path)) {
        return -1;
    }
    directory_fd = syscall4(
        SYS_OPENAT,
        AT_FDCWD,
        (isize)(u64)(const void *)"/",
        O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW,
        0
    );
    if (directory_fd < 0) {
        return -1;
    }
    while (path[index] != '\0') {
        usize count = 0;
        isize following;
        while (path[index] != '\0' && path[index] != '/') {
            if (count + 1 >= sizeof(component)) {
                syscall1(SYS_CLOSE, directory_fd);
                return -1;
            }
            component[count++] = path[index++];
        }
        component[count] = '\0';
        if (path[index] == '/') {
            index++;
            following = syscall4(
                SYS_OPENAT,
                directory_fd,
                (isize)(u64)(void *)component,
                O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW,
                0
            );
        } else {
            following = syscall4(
                SYS_OPENAT,
                directory_fd,
                (isize)(u64)(void *)component,
                final_flags | O_CLOEXEC | O_NOFOLLOW,
                0
            );
        }
        syscall1(SYS_CLOSE, directory_fd);
        if (following < 0) {
            return -1;
        }
        directory_fd = following;
    }
    return directory_fd;
}

struct sha256_state {
    u32 state[8];
    u64 bit_count;
    u8 block[64];
    usize block_count;
};

static const u32 SHA256_K[64] = {
    0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U,
    0x3956c25bU, 0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U,
    0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U,
    0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U,
    0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU,
    0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
    0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U,
    0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U,
    0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U,
    0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
    0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U,
    0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
    0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U,
    0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
    0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
    0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U,
};

static u32 rotate_right(u32 value, u32 count) {
    return (value >> count) | (value << (32U - count));
}

static void sha256_transform(struct sha256_state *state, const u8 block[64]) {
    u32 words[64];
    u32 a;
    u32 b;
    u32 c;
    u32 d;
    u32 e;
    u32 f;
    u32 g;
    u32 h;
    usize index;
    for (index = 0; index < 16; index++) {
        usize offset = index * 4;
        words[index] = ((u32)block[offset] << 24) | ((u32)block[offset + 1] << 16) |
                       ((u32)block[offset + 2] << 8) | (u32)block[offset + 3];
    }
    for (index = 16; index < 64; index++) {
        u32 s0 = rotate_right(words[index - 15], 7) ^
                 rotate_right(words[index - 15], 18) ^ (words[index - 15] >> 3);
        u32 s1 = rotate_right(words[index - 2], 17) ^
                 rotate_right(words[index - 2], 19) ^ (words[index - 2] >> 10);
        words[index] = words[index - 16] + s0 + words[index - 7] + s1;
    }
    a = state->state[0];
    b = state->state[1];
    c = state->state[2];
    d = state->state[3];
    e = state->state[4];
    f = state->state[5];
    g = state->state[6];
    h = state->state[7];
    for (index = 0; index < 64; index++) {
        u32 sum1 = rotate_right(e, 6) ^ rotate_right(e, 11) ^ rotate_right(e, 25);
        u32 choose = (e & f) ^ ((~e) & g);
        u32 first = h + sum1 + choose + SHA256_K[index] + words[index];
        u32 sum0 = rotate_right(a, 2) ^ rotate_right(a, 13) ^ rotate_right(a, 22);
        u32 majority = (a & b) ^ (a & c) ^ (b & c);
        u32 second = sum0 + majority;
        h = g;
        g = f;
        f = e;
        e = d + first;
        d = c;
        c = b;
        b = a;
        a = first + second;
    }
    state->state[0] += a;
    state->state[1] += b;
    state->state[2] += c;
    state->state[3] += d;
    state->state[4] += e;
    state->state[5] += f;
    state->state[6] += g;
    state->state[7] += h;
}

static void sha256_init(struct sha256_state *state) {
    state->state[0] = 0x6a09e667U;
    state->state[1] = 0xbb67ae85U;
    state->state[2] = 0x3c6ef372U;
    state->state[3] = 0xa54ff53aU;
    state->state[4] = 0x510e527fU;
    state->state[5] = 0x9b05688cU;
    state->state[6] = 0x1f83d9abU;
    state->state[7] = 0x5be0cd19U;
    state->bit_count = 0;
    state->block_count = 0;
}

static void sha256_update(struct sha256_state *state, const u8 *data, usize count) {
    usize index;
    for (index = 0; index < count; index++) {
        state->block[state->block_count++] = data[index];
        state->bit_count += 8;
        if (state->block_count == 64) {
            sha256_transform(state, state->block);
            state->block_count = 0;
        }
    }
}

static void sha256_final(struct sha256_state *state, u8 output[32]) {
    usize index;
    state->block[state->block_count++] = 0x80;
    if (state->block_count > 56) {
        while (state->block_count < 64) {
            state->block[state->block_count++] = 0;
        }
        sha256_transform(state, state->block);
        state->block_count = 0;
    }
    while (state->block_count < 56) {
        state->block[state->block_count++] = 0;
    }
    for (index = 0; index < 8; index++) {
        state->block[63 - index] = (u8)(state->bit_count >> (index * 8));
    }
    sha256_transform(state, state->block);
    for (index = 0; index < 8; index++) {
        output[index * 4] = (u8)(state->state[index] >> 24);
        output[index * 4 + 1] = (u8)(state->state[index] >> 16);
        output[index * 4 + 2] = (u8)(state->state[index] >> 8);
        output[index * 4 + 3] = (u8)state->state[index];
    }
}

static void digest_hex(const u8 digest[32], char output[65]) {
    static const char alphabet[] = "0123456789abcdef";
    usize index;
    for (index = 0; index < 32; index++) {
        output[index * 2] = alphabet[digest[index] >> 4];
        output[index * 2 + 1] = alphabet[digest[index] & 15];
    }
    output[64] = '\0';
}

static int hash_fd(
    isize fd,
    isize maximum_bytes,
    struct kernel_stat *metadata,
    char output[65],
    char *capture,
    usize capture_capacity
) {
    struct kernel_stat before;
    struct kernel_stat after;
    struct sha256_state state;
    u8 buffer[8192];
    isize total = 0;
    if (syscall2(SYS_FSTAT, fd, (isize)(u64)(void *)&before) < 0 ||
        (before.st_mode & S_IFMT) != S_IFREG || before.st_size <= 0 ||
        before.st_size > maximum_bytes ||
        (capture != NULL && (u64)before.st_size + 1 > capture_capacity)) {
        return 0;
    }
    sha256_init(&state);
    while (total < before.st_size) {
        isize wanted = before.st_size - total;
        isize observed;
        if (wanted > (isize)sizeof(buffer)) {
            wanted = sizeof(buffer);
        }
        observed = syscall3(SYS_READ, fd, (isize)(u64)(void *)buffer, wanted);
        if (observed <= 0 || observed > wanted) {
            return 0;
        }
        sha256_update(&state, buffer, (usize)observed);
        if (capture != NULL) {
            bytes_copy(capture + total, (const char *)buffer, (usize)observed);
        }
        total += observed;
    }
    if (syscall2(SYS_FSTAT, fd, (isize)(u64)(void *)&after) < 0 ||
        !metadata_equal(&before, &after)) {
        return 0;
    }
    if (capture != NULL) {
        capture[total] = '\0';
    }
    {
        u8 digest[32];
        sha256_final(&state, digest);
        digest_hex(digest, output);
    }
    *metadata = before;
    return 1;
}

struct slice {
    const char *data;
    usize count;
};

struct json_cursor {
    const char *current;
    const char *end;
};

static int skip_json_value(struct json_cursor *cursor, unsigned depth);

static int skip_json_string(struct json_cursor *cursor) {
    if (cursor->current >= cursor->end || *cursor->current++ != '"') {
        return 0;
    }
    while (cursor->current < cursor->end) {
        u8 value = (u8)*cursor->current++;
        if (value == '"') {
            return 1;
        }
        if (value < 0x20) {
            return 0;
        }
        if (value == '\\') {
            usize index;
            if (cursor->current >= cursor->end) {
                return 0;
            }
            value = (u8)*cursor->current++;
            if (value == 'u') {
                for (index = 0; index < 4; index++) {
                    if (cursor->current >= cursor->end ||
                        !(((u8)*cursor->current >= '0' && (u8)*cursor->current <= '9') ||
                          ((u8)*cursor->current >= 'a' && (u8)*cursor->current <= 'f') ||
                          ((u8)*cursor->current >= 'A' && (u8)*cursor->current <= 'F'))) {
                        return 0;
                    }
                    cursor->current++;
                }
            } else if (!(value == '"' || value == '\\' || value == '/' || value == 'b' ||
                         value == 'f' || value == 'n' || value == 'r' || value == 't')) {
                return 0;
            }
        }
    }
    return 0;
}

static int skip_json_number(struct json_cursor *cursor) {
    const char *start = cursor->current;
    if (cursor->current < cursor->end && *cursor->current == '-') {
        cursor->current++;
    }
    if (cursor->current >= cursor->end) {
        return 0;
    }
    if (*cursor->current == '0') {
        cursor->current++;
        if (cursor->current < cursor->end && *cursor->current >= '0' &&
            *cursor->current <= '9') {
            return 0;
        }
    } else {
        if (*cursor->current < '1' || *cursor->current > '9') {
            return 0;
        }
        while (cursor->current < cursor->end && *cursor->current >= '0' &&
               *cursor->current <= '9') {
            cursor->current++;
        }
    }
    if (cursor->current < cursor->end && *cursor->current == '.') {
        cursor->current++;
        if (cursor->current >= cursor->end || *cursor->current < '0' ||
            *cursor->current > '9') {
            return 0;
        }
        while (cursor->current < cursor->end && *cursor->current >= '0' &&
               *cursor->current <= '9') {
            cursor->current++;
        }
    }
    if (cursor->current < cursor->end &&
        (*cursor->current == 'e' || *cursor->current == 'E')) {
        cursor->current++;
        if (cursor->current < cursor->end &&
            (*cursor->current == '+' || *cursor->current == '-')) {
            cursor->current++;
        }
        if (cursor->current >= cursor->end || *cursor->current < '0' ||
            *cursor->current > '9') {
            return 0;
        }
        while (cursor->current < cursor->end && *cursor->current >= '0' &&
               *cursor->current <= '9') {
            cursor->current++;
        }
    }
    return cursor->current > start;
}

static int skip_json_value(struct json_cursor *cursor, unsigned depth) {
    if (depth > MAX_JSON_DEPTH || cursor->current >= cursor->end) {
        return 0;
    }
    if (*cursor->current == '"') {
        return skip_json_string(cursor);
    }
    if (*cursor->current == '{') {
        cursor->current++;
        if (cursor->current < cursor->end && *cursor->current == '}') {
            cursor->current++;
            return 1;
        }
        while (cursor->current < cursor->end) {
            if (!skip_json_string(cursor) || cursor->current >= cursor->end ||
                *cursor->current++ != ':' || !skip_json_value(cursor, depth + 1)) {
                return 0;
            }
            if (cursor->current >= cursor->end) {
                return 0;
            }
            if (*cursor->current == '}') {
                cursor->current++;
                return 1;
            }
            if (*cursor->current++ != ',') {
                return 0;
            }
        }
        return 0;
    }
    if (*cursor->current == '[') {
        cursor->current++;
        if (cursor->current < cursor->end && *cursor->current == ']') {
            cursor->current++;
            return 1;
        }
        while (cursor->current < cursor->end) {
            if (!skip_json_value(cursor, depth + 1) || cursor->current >= cursor->end) {
                return 0;
            }
            if (*cursor->current == ']') {
                cursor->current++;
                return 1;
            }
            if (*cursor->current++ != ',') {
                return 0;
            }
        }
        return 0;
    }
    if ((usize)(cursor->end - cursor->current) >= 4 &&
        bytes_equal(cursor->current, "true", 4)) {
        cursor->current += 4;
        return 1;
    }
    if ((usize)(cursor->end - cursor->current) >= 5 &&
        bytes_equal(cursor->current, "false", 5)) {
        cursor->current += 5;
        return 1;
    }
    if ((usize)(cursor->end - cursor->current) >= 4 &&
        bytes_equal(cursor->current, "null", 4)) {
        cursor->current += 4;
        return 1;
    }
    return skip_json_number(cursor);
}

static int parse_ascii_key(struct json_cursor *cursor, char *output, usize capacity) {
    usize count = 0;
    if (cursor->current >= cursor->end || *cursor->current++ != '"') {
        return 0;
    }
    while (cursor->current < cursor->end && *cursor->current != '"') {
        u8 value = (u8)*cursor->current++;
        if (value < 0x21 || value > 0x7e || value == '\\' || count + 1 >= capacity) {
            return 0;
        }
        output[count++] = (char)value;
    }
    if (cursor->current >= cursor->end || *cursor->current++ != '"') {
        return 0;
    }
    output[count] = '\0';
    return 1;
}

static int find_object_member(
    struct slice object,
    const char *wanted,
    struct slice *result
) {
    struct json_cursor cursor;
    unsigned found = 0;
    cursor.current = object.data;
    cursor.end = object.data + object.count;
    if (cursor.current >= cursor.end || *cursor.current++ != '{') {
        return 0;
    }
    if (cursor.current < cursor.end && *cursor.current == '}') {
        cursor.current++;
        return 0;
    }
    while (cursor.current < cursor.end) {
        char key[96];
        const char *value_start;
        if (!parse_ascii_key(&cursor, key, sizeof(key)) || cursor.current >= cursor.end ||
            *cursor.current++ != ':') {
            return 0;
        }
        value_start = cursor.current;
        if (!skip_json_value(&cursor, 1)) {
            return 0;
        }
        if (string_equal(key, wanted)) {
            found++;
            result->data = value_start;
            result->count = (usize)(cursor.current - value_start);
        }
        if (cursor.current >= cursor.end) {
            return 0;
        }
        if (*cursor.current == '}') {
            cursor.current++;
            return found == 1 && cursor.current == cursor.end;
        }
        if (*cursor.current++ != ',') {
            return 0;
        }
    }
    return 0;
}

static int slice_u64(struct slice value, u64 *output) {
    usize index;
    u64 result = 0;
    if (value.count == 0 || (value.count > 1 && value.data[0] == '0')) {
        return 0;
    }
    for (index = 0; index < value.count; index++) {
        u64 digit;
        if (value.data[index] < '0' || value.data[index] > '9') {
            return 0;
        }
        digit = (u64)(value.data[index] - '0');
        if (result > (0xffffffffffffffffUL - digit) / 10UL) {
            return 0;
        }
        result = result * 10UL + digit;
    }
    *output = result;
    return 1;
}

static int slice_bool(struct slice value, int *output) {
    if (value.count == 4 && bytes_equal(value.data, "true", 4)) {
        *output = 1;
        return 1;
    }
    if (value.count == 5 && bytes_equal(value.data, "false", 5)) {
        *output = 0;
        return 1;
    }
    return 0;
}

static int slice_ascii_string(struct slice value, char *output, usize capacity) {
    usize index;
    if (value.count < 2 || value.data[0] != '"' || value.data[value.count - 1] != '"' ||
        value.count - 1 > capacity) {
        return 0;
    }
    for (index = 1; index + 1 < value.count; index++) {
        u8 byte = (u8)value.data[index];
        if (byte < 0x20 || byte > 0x7e || byte == '\\' || byte == '"') {
            return 0;
        }
        output[index - 1] = (char)byte;
    }
    output[value.count - 2] = '\0';
    return 1;
}

struct launch_binding {
    char path[MAX_PATH_BYTES];
    char resolved_path[MAX_PATH_BYTES];
    char sha256[65];
    u64 byte_count;
    u64 owner_uid;
    u64 owner_gid;
    u64 mode;
    u64 nlink;
    char source_path[MAX_PATH_BYTES];
    char source_sha256[65];
    char runner_cli_path[MAX_PATH_BYTES];
    char smoke_packet_path[MAX_PATH_BYTES];
    char model_config_manifest_path[MAX_PATH_BYTES];
};

static const char *const LAUNCH_KEYS[] = {
    "bootstrap_byte_count",
    "bootstrap_sha256",
    "byte_count",
    "confirmation",
    "dt_needed_allowed",
    "elf_machine",
    "elf_type",
    "executable_stack",
    "fini_array_allowed",
    "init_array_allowed",
    "mode",
    "model_config_manifest_path",
    "nlink",
    "owner_gid",
    "owner_uid",
    "path",
    "pt_dynamic_allowed",
    "pt_interp_allowed",
    "resolved_path",
    "rpath_runpath_allowed",
    "runner_cli_path",
    "schema_version",
    "sha256",
    "shell_option",
    "smoke_packet_path",
    "source_path",
    "source_sha256",
    "static",
    "tls_segment_allowed",
    "token_prefix",
    "writable_executable_segment_allowed",
};

#define LAUNCH_KEY_COUNT (sizeof(LAUNCH_KEYS) / sizeof(LAUNCH_KEYS[0]))

static int launch_key_index(const char *key) {
    usize index;
    for (index = 0; index < LAUNCH_KEY_COUNT; index++) {
        if (string_equal(key, LAUNCH_KEYS[index])) {
            return (int)index;
        }
    }
    return -1;
}

static int exact_slice_string(struct slice value, const char *expected) {
    usize count = string_length(expected);
    return value.count == count + 2 && value.data[0] == '"' &&
           value.data[value.count - 1] == '"' && bytes_equal(value.data + 1, expected, count);
}

static int valid_sha_string(const char *value) {
    usize index;
    if (string_length(value) != SHA256_HEX_BYTES) {
        return 0;
    }
    for (index = 0; index < SHA256_HEX_BYTES; index++) {
        if (!is_lower_hex(value[index])) {
            return 0;
        }
    }
    return 1;
}

static const char *const AUTHORITY_KEYS[] = {
    "authority_id",
    "authorized",
    "authorized_scope",
    "bindings",
    "client_runtime",
    "decision_id",
    "endpoint",
    "evidence_root",
    "expires_at_utc",
    "gpu",
    "issued_at_utc",
    "launch_shim",
    "matrix",
    "model_order",
    "models",
    "network_namespace",
    "outer_runtime",
    "owner_uid",
    "policies",
    "private_runtime",
    "runtime_scratch_root",
    "schema_version",
    "server_runtime",
    "source",
    "tool_shell",
};

#define AUTHORITY_KEY_COUNT (sizeof(AUTHORITY_KEYS) / sizeof(AUTHORITY_KEYS[0]))

static int authority_key_index(const char *key) {
    usize index;
    for (index = 0; index < AUTHORITY_KEY_COUNT; index++) {
        if (string_equal(key, AUTHORITY_KEYS[index])) {
            return (int)index;
        }
    }
    return -1;
}

static int parse_authority_subject(
    struct slice object,
    struct slice *launch_object,
    struct slice *outer_runtime,
    struct slice *source_object,
    struct slice *bindings_object
) {
    struct json_cursor cursor;
    u64 seen = 0;
    cursor.current = object.data;
    cursor.end = object.data + object.count;
    if (cursor.current >= cursor.end || *cursor.current++ != '{') {
        return 0;
    }
    while (cursor.current < cursor.end && *cursor.current != '}') {
        char key[96];
        const char *value_start;
        struct slice value;
        int key_index;
        int boolean_value;
        u64 integer_value;
        if (!parse_ascii_key(&cursor, key, sizeof(key)) || cursor.current >= cursor.end ||
            *cursor.current++ != ':') {
            return 0;
        }
        key_index = authority_key_index(key);
        if (key_index < 0 || (seen & (1UL << (unsigned)key_index)) != 0 ||
            (key_index > 0 && (seen & (1UL << (unsigned)(key_index - 1))) == 0)) {
            return 0;
        }
        value_start = cursor.current;
        if (!skip_json_value(&cursor, 1)) {
            return 0;
        }
        value.data = value_start;
        value.count = (usize)(cursor.current - value_start);
        seen |= 1UL << (unsigned)key_index;
        switch (key_index) {
            case 1:
                if (!slice_bool(value, &boolean_value) || boolean_value != 1) return 0;
                break;
            case 2:
                if (!exact_slice_string(value, AUTHORIZED_SCOPE)) return 0;
                break;
            case 3:
                *bindings_object = value;
                break;
            case 5:
                if (!exact_slice_string(value, "D-034")) return 0;
                break;
            case 11:
                *launch_object = value;
                break;
            case 16:
                *outer_runtime = value;
                break;
            case 17:
                if (!slice_u64(value, &integer_value) || integer_value != 0) return 0;
                break;
            case 21:
                if (!exact_slice_string(value, AUTHORITY_SCHEMA)) return 0;
                break;
            case 23:
                *source_object = value;
                break;
            default:
                break;
        }
        if (cursor.current < cursor.end && *cursor.current == ',') {
            cursor.current++;
        } else {
            break;
        }
    }
    return cursor.current < cursor.end && *cursor.current++ == '}' &&
           cursor.current == cursor.end &&
           seen == ((1UL << AUTHORITY_KEY_COUNT) - 1UL);
}

static int parse_launch_binding(struct slice object, struct launch_binding *binding) {
    struct json_cursor cursor;
    u64 seen = 0;
    cursor.current = object.data;
    cursor.end = object.data + object.count;
    if (cursor.current >= cursor.end || *cursor.current++ != '{') {
        return 0;
    }
    while (cursor.current < cursor.end && *cursor.current != '}') {
        char key[96];
        const char *value_start;
        struct slice value;
        int key_index;
        int boolean_value;
        u64 integer_value;
        if (!parse_ascii_key(&cursor, key, sizeof(key)) || cursor.current >= cursor.end ||
            *cursor.current++ != ':') {
            return 0;
        }
        key_index = launch_key_index(key);
        if (key_index < 0 || (seen & (1UL << (unsigned)key_index)) != 0) {
            return 0;
        }
        if (key_index > 0 && (seen & (1UL << (unsigned)(key_index - 1))) == 0) {
            /* Canonical authority objects have lexicographically sorted keys. */
            return 0;
        }
        value_start = cursor.current;
        if (!skip_json_value(&cursor, 1)) {
            return 0;
        }
        value.data = value_start;
        value.count = (usize)(cursor.current - value_start);
        seen |= 1UL << (unsigned)key_index;
        switch (key_index) {
            case 0:
                if (!slice_u64(value, &integer_value) || integer_value != 4645) return 0;
                break;
            case 1:
                if (!exact_slice_string(value, BOOTSTRAP_SHA256)) return 0;
                break;
            case 2:
                if (!slice_u64(value, &binding->byte_count) || binding->byte_count == 0) return 0;
                break;
            case 3:
                if (!exact_slice_string(value, CONFIRMATION)) return 0;
                break;
            case 4:
            case 7:
            case 8:
            case 9:
            case 16:
            case 17:
            case 19:
            case 28:
            case 30:
                if (!slice_bool(value, &boolean_value) || boolean_value != 0) return 0;
                break;
            case 5:
                if (!exact_slice_string(value, "EM_X86_64")) return 0;
                break;
            case 6:
                if (!exact_slice_string(value, "ET_EXEC")) return 0;
                break;
            case 10:
                if (!slice_u64(value, &binding->mode)) return 0;
                break;
            case 11:
                if (!slice_ascii_string(value, binding->model_config_manifest_path,
                                        sizeof(binding->model_config_manifest_path))) return 0;
                break;
            case 12:
                if (!slice_u64(value, &binding->nlink)) return 0;
                break;
            case 13:
                if (!slice_u64(value, &binding->owner_gid)) return 0;
                break;
            case 14:
                if (!slice_u64(value, &binding->owner_uid)) return 0;
                break;
            case 15:
                if (!slice_ascii_string(value, binding->path, sizeof(binding->path))) return 0;
                break;
            case 18:
                if (!slice_ascii_string(value, binding->resolved_path,
                                        sizeof(binding->resolved_path))) return 0;
                break;
            case 20:
                if (!slice_ascii_string(value, binding->runner_cli_path,
                                        sizeof(binding->runner_cli_path))) return 0;
                break;
            case 21:
                if (!exact_slice_string(value, SHIM_SCHEMA)) return 0;
                break;
            case 22:
                if (!slice_ascii_string(value, binding->sha256, sizeof(binding->sha256))) return 0;
                break;
            case 23:
                if (!exact_slice_string(value, "-c")) return 0;
                break;
            case 24:
                if (!slice_ascii_string(value, binding->smoke_packet_path,
                                        sizeof(binding->smoke_packet_path))) return 0;
                break;
            case 25:
                if (!slice_ascii_string(value, binding->source_path,
                                        sizeof(binding->source_path))) return 0;
                break;
            case 26:
                if (!slice_ascii_string(value, binding->source_sha256,
                                        sizeof(binding->source_sha256))) return 0;
                break;
            case 27:
                if (!slice_bool(value, &boolean_value) || boolean_value != 1) return 0;
                break;
            case 29:
                if (!exact_slice_string(value, TOKEN_PREFIX)) return 0;
                break;
            default:
                return 0;
        }
        if (cursor.current < cursor.end && *cursor.current == ',') {
            cursor.current++;
        } else {
            break;
        }
    }
    if (cursor.current >= cursor.end || *cursor.current++ != '}' || cursor.current != cursor.end ||
        seen != ((1UL << LAUNCH_KEY_COUNT) - 1UL)) {
        return 0;
    }
    if (!(string_equal(binding->path, SHIM_PATH) &&
          string_equal(binding->resolved_path, SHIM_PATH) &&
          binding->owner_uid == 1035 && binding->owner_gid == 1035 &&
          binding->mode == 0500 && binding->nlink == 1 &&
          valid_sha_string(binding->sha256) && valid_sha_string(binding->source_sha256) &&
          safe_absolute_path(binding->source_path) &&
          safe_absolute_path(binding->runner_cli_path) &&
          safe_absolute_path(binding->smoke_packet_path) &&
          safe_absolute_path(binding->model_config_manifest_path))) {
        return 0;
    }
    return 1;
}

static int object_string(
    struct slice object,
    const char *key,
    char *output,
    usize capacity
) {
    struct slice value;
    return find_object_member(object, key, &value) &&
           slice_ascii_string(value, output, capacity);
}

static int object_u64(struct slice object, const char *key, u64 *output) {
    struct slice value;
    return find_object_member(object, key, &value) && slice_u64(value, output);
}

static int verify_file(
    const char *path,
    const char *expected_sha256,
    isize expected_byte_count,
    u32 expected_uid,
    u32 expected_gid,
    u32 expected_mode,
    u64 expected_nlink,
    isize maximum_bytes
) {
    isize fd = open_absolute_nofollow(path, O_RDONLY);
    struct kernel_stat metadata;
    char digest[65];
    int result;
    if (fd < 0) {
        return 0;
    }
    result = hash_fd(fd, maximum_bytes, &metadata, digest, NULL, 0) &&
             string_equal(digest, expected_sha256) &&
             (expected_byte_count < 0 || metadata.st_size == expected_byte_count) &&
             metadata.st_uid == expected_uid &&
             metadata.st_gid == expected_gid && (metadata.st_mode & S_IFMT) == S_IFREG &&
             (metadata.st_mode & MODE_MASK) == expected_mode &&
             metadata.st_nlink == expected_nlink;
    syscall1(SYS_CLOSE, fd);
    return result;
}

static int parse_token(char *token, char **authority_path, char **authority_sha256) {
    char *first = NULL;
    char *second = NULL;
    usize index;
    usize delimiters = 0;
    usize length = string_length(token);
    if (length < string_length(TOKEN_PREFIX) + 68 || length >= MAX_PATH_BYTES + 96) {
        return 0;
    }
    for (index = 0; index < length; index++) {
        u8 value = (u8)token[index];
        if (value == ':') {
            delimiters++;
            if (delimiters == 1) first = token + index;
            if (delimiters == 2) second = token + index;
        }
        if (value <= 0x20 || value > 0x7e) {
            return 0;
        }
    }
    if (delimiters != 2 || first == NULL || second == NULL) {
        return 0;
    }
    *first = '\0';
    *second = '\0';
    *authority_path = first + 1;
    *authority_sha256 = second + 1;
    if (!string_equal(token, TOKEN_PREFIX) || !string_equal(*authority_path, AUTHORITY_PATH) ||
        !safe_absolute_path(*authority_path) || !valid_sha_string(*authority_sha256)) {
        return 0;
    }
    return 1;
}

static int pre_gate_environment_allowed(char **environment) {
    usize count = 0;
    usize total = 0;
    usize required_ld_library_path_count = 0;
    while (count < 4096 && environment[count] != NULL) {
        const char *entry = environment[count];
        usize length;
        if (!bounded_string_length(entry, 65536, &length) ||
            total > 1048576 - length) {
            return 0;
        }
        total += length;
        if (string_equal(entry, "LD_LIBRARY_PATH=/usr/local/cuda-13.0/lib64")) {
            required_ld_library_path_count++;
        } else if (string_starts_with(entry, "LD_") ||
                   string_starts_with(entry, "BASH_FUNC_") ||
                   environment_name_equal(entry, "GLIBC_TUNABLES") ||
                   environment_name_equal(entry, "GCONV_PATH") ||
                   environment_name_equal(entry, "ENV") ||
                   environment_name_equal(entry, "BASH_ENV") ||
                   environment_name_equal(entry, "SHELLOPTS") ||
                   environment_name_equal(entry, "IFS")) {
            return 0;
        }
        count++;
    }
    return !(count == 4096 && environment[count] != NULL) &&
           required_ld_library_path_count == 1;
}

static char AUTHORITY_BYTES[MAX_AUTHORITY_BYTES + 1];

static const char FROZEN_BOOTSTRAP[] =
    "import hashlib,json,os,stat,sys\ndef read_exact(path,limit):\n    if not path.startswith('/') or os.path.normpath(path)!=path or '..' in path.split('/'):\n        raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')\n    parts=path.split('/')[1:];directory_fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC|os.O_NOFOLLOW)\n    try:\n        for part in parts[:-1]:\n            following=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=directory_fd);os.close(directory_fd);directory_fd=following\n        fd=os.open(parts[-1],os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=directory_fd)\n    finally:\n        os.close(directory_fd)\n    try:\n        before=os.fstat(fd)\n        if not stat.S_ISREG(before.st_mode) or not 0<before.st_size<=limit:\n            raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')\n        chunks=[]\n        left=before.st_size\n        while left:\n            chunk=os.read(fd,min(left,1048576))\n            if not chunk:\n                raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')\n            chunks.append(chunk);left-=len(chunk)\n        after=os.fstat(fd)\n    finally:\n        os.close(fd)\n    identity=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)\n    if identity(before)!=identity(after):\n        raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')\n    return b''.join(chunks),before\nif not (sys.flags.isolated==1 and sys.flags.ignore_environment==1 and sys.flags.no_site==1 and sys.flags.dont_write_bytecode==1 and sys.pycache_prefix=='/dev/null'):\n    raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')\nstage,authority_path,authority_sha,bootstrap_sha,cli_path,stage_payload,*cli_args=sys.argv[1:]\ninternal_flags={'--inside-network-namespace-stage1','--inside-network-namespace','--namespace-sandboxed','--stage1-receipt-b64','--stage0-bootstrap-sha256','--pinned-bootstrap-stage'}\nif stage not in {'STAGE0','STAGE1','STAGE2'} or any(any(item==flag or item.startswith(flag+'=') for flag in internal_flags) for item in cli_args):\n    raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')\nauthority_bytes,authority_meta=read_exact(authority_path,131072)\nif hashlib.sha256(authority_bytes).hexdigest()!=authority_sha:\n    raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')\ndef closed_pairs(pairs):\n    result={}\n    for key,value in pairs:\n        if key in result: raise ValueError('duplicate')\n        result[key]=value\n    return result\nauthority=json.loads(authority_bytes,object_pairs_hook=closed_pairs)\ncanonical=json.dumps(authority,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()\nnamespace=authority['network_namespace'];source=authority['source']\nexpected_environment={'LC_CTYPE':'C.UTF-8'} if stage in {'STAGE0','STAGE1'} else namespace['launcher_environment']\nexpected_uid=namespace['host_owner_uid'] if stage=='STAGE0' else namespace['inside_owner_uid']\nexpected_gid=namespace['host_owner_gid'] if stage=='STAGE0' else namespace['inside_owner_gid']\nif not (canonical==authority_bytes and dict(os.environ)==expected_environment and authority_meta.st_uid==expected_uid and authority_meta.st_gid==expected_gid and stat.S_IMODE(authority_meta.st_mode)==0o600 and authority_meta.st_nlink==1 and bootstrap_sha==source['outer_bootstrap_code_sha256']):\n    raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')\nbinding=source['critical_files']['runner_cli']\nexpected_cli=source['worktree_root']+'/'+binding['relative_path']\ncli_bytes,cli_meta=read_exact(cli_path,1048576)\nif not (cli_path==expected_cli and hashlib.sha256(cli_bytes).hexdigest()==binding['sha256'] and cli_meta.st_uid==expected_uid and cli_meta.st_gid==expected_gid and cli_meta.st_nlink==1 and stat.S_IMODE(cli_meta.st_mode)&0o022==0):\n    raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')\nstage_args={'STAGE0':[],'STAGE1':['--inside-network-namespace-stage1'],'STAGE2':['--inside-network-namespace','--namespace-sandboxed','--stage1-receipt-b64',stage_payload]}[stage]\nif (stage in {'STAGE0','STAGE1'} and stage_payload!='-') or (stage=='STAGE2' and not 0<len(stage_payload)<=262144):\n    raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')\nsys.argv=[cli_path,*cli_args,'--stage0-bootstrap-sha256',bootstrap_sha,'--pinned-bootstrap-stage',stage,*stage_args]\nscope={'__name__':'__main__','__file__':cli_path,'__package__':None,'__cached__':None,'__builtins__':__builtins__,'_PINNED_BOOTSTRAP':{'authority_sha256':authority_sha,'cli_sha256':binding['sha256'],'bootstrap_sha256':bootstrap_sha,'stage':stage,'cli_opened_nofollow':True,'cli_compiled_from_verified_bytes':True}}\nexec(compile(cli_bytes,cli_path,'exec'),scope,scope)\n";

static int bootstrap_matches_binding(void) {
    struct sha256_state state;
    u8 digest[32];
    char digest_string[65];
    usize count = sizeof(FROZEN_BOOTSTRAP) - 1;
    if (count != 4645) {
        return 0;
    }
    sha256_init(&state);
    sha256_update(&state, (const u8 *)FROZEN_BOOTSTRAP, count);
    sha256_final(&state, digest);
    digest_hex(digest, digest_string);
    return string_equal(digest_string, BOOTSTRAP_SHA256);
}

static int read_self_path(char output[MAX_PATH_BYTES]) {
    isize count = syscall4(
        SYS_READLINKAT,
        AT_FDCWD,
        (isize)(u64)(const void *)"/proc/self/exe",
        (isize)(u64)(void *)output,
        MAX_PATH_BYTES - 1
    );
    if (count <= 0 || count >= MAX_PATH_BYTES) {
        return 0;
    }
    output[count] = '\0';
    return string_equal(output, SHIM_PATH);
}

static void clear_inherited_environment(char **environment) {
    usize count = 0;
    usize index;
    usize total = 0;
    while (count < 4096 && environment[count] != NULL) {
        usize length;
        if (!bounded_string_length(environment[count], 65536, &length) ||
            total > 1048576 - length) {
            fail();
        }
        total += length;
        count++;
    }
    if (count == 4096 && environment[count] != NULL) {
        fail();
    }
    for (index = 0; index < count; index++) {
        usize offset;
        usize length;
        if (!bounded_string_length(environment[index], 65536, &length)) {
            fail();
        }
        for (offset = 0; offset < length; offset++) {
            environment[index][offset] = '\0';
        }
        environment[index] = NULL;
    }
}

static int standard_descriptors_are_non_socket(void) {
    isize descriptor;
    for (descriptor = 0; descriptor <= 2; descriptor++) {
        struct kernel_stat metadata;
        if (syscall2(SYS_FSTAT, descriptor, (isize)(u64)(void *)&metadata) < 0 ||
            (metadata.st_mode & S_IFMT) == S_IFSOCK) {
            return 0;
        }
    }
    return 1;
}

__attribute__((noreturn, used)) void shim_main(u64 *initial_stack) {
    isize argc = (isize)initial_stack[0];
    char **argv = (char **)(void *)(initial_stack + 1);
    char **environment;
    usize argv0_length;
    usize argv1_length;
    usize token_length;
    char *authority_path;
    char *authority_sha256;
    isize authority_fd;
    struct kernel_stat authority_metadata;
    char authority_digest[65];
    struct slice authority_object;
    struct slice launch_object;
    struct slice outer_runtime;
    struct slice source_object;
    struct slice bindings_object;
    struct launch_binding launch;
    char python_path[MAX_PATH_BYTES];
    char python_resolved_path[MAX_PATH_BYTES];
    char python_sha256[65];
    u64 python_byte_count;
    u64 python_owner_uid;
    u64 python_owner_gid;
    u64 python_mode;
    char source_bootstrap_sha256[65];
    u64 source_bootstrap_byte_count;
    char packet_sha256[65];
    char manifest_sha256[65];
    char runner_cli_sha256[65];
    char self_path[MAX_PATH_BYTES];
    isize self_fd;
    struct kernel_stat self_metadata;
    char self_sha256[65];
    isize python_fd;
    struct kernel_stat python_metadata;
    char observed_python_sha256[65];
    char *python_argv[28];
    char *python_environment[2];

    /* Validate the kernel-provided vector boundary before deriving envp. */
    if (argc != 3 || argv[0] == NULL || argv[1] == NULL || argv[2] == NULL ||
        argv[3] != NULL ||
        !bounded_string_length(argv[0], MAX_PATH_BYTES - 1, &argv0_length) ||
        !bounded_string_length(argv[1], 2, &argv1_length) ||
        !bounded_string_length(argv[2], MAX_PATH_BYTES + 95, &token_length) ||
        argv0_length != sizeof(SHIM_PATH) - 1 || argv1_length != 2 ||
        token_length < sizeof(TOKEN_PREFIX) + 67) {
        fail();
    }
    environment = argv + 4;

    /* No inherited environment is consulted by any subsequent shim operation. */
    if (!pre_gate_environment_allowed(environment)) {
        fail();
    }
    clear_inherited_environment(environment);
    if (syscall3(SYS_CLOSE_RANGE, 3, 0xffffffffUL, 0) < 0 ||
        !standard_descriptors_are_non_socket() ||
        !string_equal(argv[0], SHIM_PATH) || !string_equal(argv[1], "-c") ||
        !parse_token(argv[2], &authority_path, &authority_sha256)) {
        fail();
    }

    authority_fd = open_absolute_nofollow(authority_path, O_RDONLY);
    if (authority_fd < 0 ||
        !hash_fd(
            authority_fd,
            MAX_AUTHORITY_BYTES,
            &authority_metadata,
            authority_digest,
            AUTHORITY_BYTES,
            sizeof(AUTHORITY_BYTES)
        ) ||
        !string_equal(authority_digest, authority_sha256) ||
        authority_metadata.st_uid != 1035 || authority_metadata.st_gid != 1035 ||
        (authority_metadata.st_mode & MODE_MASK) != 0600 ||
        authority_metadata.st_nlink != 1) {
        fail();
    }
    syscall1(SYS_CLOSE, authority_fd);
    authority_object.data = AUTHORITY_BYTES;
    authority_object.count = (usize)authority_metadata.st_size;
    if (!parse_authority_subject(
            authority_object,
            &launch_object,
            &outer_runtime,
            &source_object,
            &bindings_object
        ) ||
        !parse_launch_binding(launch_object, &launch)) {
        fail();
    }

    if (!object_string(outer_runtime, "python_path", python_path, sizeof(python_path)) ||
        !object_string(outer_runtime, "python_resolved_path", python_resolved_path,
                       sizeof(python_resolved_path)) ||
        !object_string(outer_runtime, "python_sha256", python_sha256,
                       sizeof(python_sha256)) ||
        !object_u64(outer_runtime, "python_byte_count", &python_byte_count) ||
        !object_u64(outer_runtime, "required_owner_uid", &python_owner_uid) ||
        !object_u64(outer_runtime, "required_owner_gid", &python_owner_gid) ||
        !object_u64(outer_runtime, "executable_mode", &python_mode) ||
        !string_equal(python_path, "/usr/bin/python3.10") ||
        !string_equal(python_resolved_path, python_path) || !valid_sha_string(python_sha256) ||
        python_owner_uid != D034_OUTER_PYTHON_UID ||
        python_owner_gid != D034_OUTER_PYTHON_GID || python_mode != 0755) {
        fail();
    }
    if (!object_string(source_object, "outer_bootstrap_code_sha256",
                       source_bootstrap_sha256, sizeof(source_bootstrap_sha256)) ||
        !object_u64(source_object, "outer_bootstrap_code_byte_count",
                    &source_bootstrap_byte_count) ||
        !string_equal(source_bootstrap_sha256, BOOTSTRAP_SHA256) ||
        source_bootstrap_byte_count != 4645 || !bootstrap_matches_binding()) {
        fail();
    }
    if (!object_string(bindings_object, "smoke_packet_sha256", packet_sha256,
                       sizeof(packet_sha256)) ||
        !object_string(bindings_object, "model_config_manifest_sha256", manifest_sha256,
                       sizeof(manifest_sha256)) ||
        !object_string(bindings_object, "runner_cli_sha256", runner_cli_sha256,
                       sizeof(runner_cli_sha256)) ||
        !valid_sha_string(packet_sha256) || !valid_sha_string(manifest_sha256) ||
        !valid_sha_string(runner_cli_sha256)) {
        fail();
    }

    self_fd = syscall4(
        SYS_OPENAT,
        AT_FDCWD,
        (isize)(u64)(const void *)"/proc/self/exe",
        O_RDONLY | O_CLOEXEC,
        0
    );
    if (self_fd < 0 || !read_self_path(self_path) ||
        !hash_fd(self_fd, 4 * 1024 * 1024, &self_metadata, self_sha256, NULL, 0) ||
        !string_equal(self_sha256, launch.sha256) ||
        self_metadata.st_size != (isize)launch.byte_count || self_metadata.st_uid != 1035 ||
        self_metadata.st_gid != 1035 || (self_metadata.st_mode & MODE_MASK) != 0500 ||
        self_metadata.st_nlink != 1) {
        fail();
    }
    syscall1(SYS_CLOSE, self_fd);

    if (!verify_file(launch.source_path, launch.source_sha256, -1, 1035, 1035, 0400, 1,
                     2 * 1024 * 1024) ||
        !verify_file(launch.runner_cli_path, runner_cli_sha256, -1, 1035, 1035, 0400, 1,
                     2 * 1024 * 1024) ||
        !verify_file(launch.smoke_packet_path, packet_sha256, -1, 1035, 1035, 0600, 1,
                     2 * 1024 * 1024) ||
        !verify_file(launch.model_config_manifest_path, manifest_sha256, -1, 1035, 1035,
                     0400, 1, 2 * 1024 * 1024)) {
        fail();
    }

    python_fd = open_absolute_nofollow(python_path, O_RDONLY);
    if (python_fd < 0 ||
        !hash_fd(python_fd, 16 * 1024 * 1024, &python_metadata, observed_python_sha256,
                 NULL, 0) ||
        !string_equal(observed_python_sha256, python_sha256) ||
        python_metadata.st_size != (isize)python_byte_count ||
        python_metadata.st_uid != D034_OUTER_PYTHON_UID ||
        python_metadata.st_gid != D034_OUTER_PYTHON_GID ||
        (python_metadata.st_mode & MODE_MASK) != 0755 ||
        python_metadata.st_nlink != 1) {
        fail();
    }

    python_argv[0] = python_path;
    python_argv[1] = "-I";
    python_argv[2] = "-S";
    python_argv[3] = "-B";
    python_argv[4] = "-X";
    python_argv[5] = "pycache_prefix=/dev/null";
    python_argv[6] = "-c";
    python_argv[7] = (char *)(void *)FROZEN_BOOTSTRAP;
    python_argv[8] = "STAGE0";
    python_argv[9] = authority_path;
    python_argv[10] = authority_sha256;
    python_argv[11] = (char *)(void *)BOOTSTRAP_SHA256;
    python_argv[12] = launch.runner_cli_path;
    python_argv[13] = "-";
    python_argv[14] = "execute";
    python_argv[15] = "--authority";
    python_argv[16] = authority_path;
    python_argv[17] = "--authority-sha256";
    python_argv[18] = authority_sha256;
    python_argv[19] = "--smoke-packet";
    python_argv[20] = launch.smoke_packet_path;
    python_argv[21] = "--model-config-manifest";
    python_argv[22] = launch.model_config_manifest_path;
    python_argv[23] = "--confirm-execute";
    python_argv[24] = (char *)(void *)CONFIRMATION;
    python_argv[25] = NULL;
    python_environment[0] = "LC_CTYPE=C.UTF-8";
    python_environment[1] = NULL;
#ifdef D034_TEST_EXEC_BOUNDARY
    if (!(string_equal(python_argv[0], "/usr/bin/python3.10") &&
          string_equal(python_argv[1], "-I") && string_equal(python_argv[2], "-S") &&
          string_equal(python_argv[3], "-B") && string_equal(python_argv[4], "-X") &&
          string_equal(python_argv[5], "pycache_prefix=/dev/null") &&
          string_equal(python_argv[6], "-c") &&
          string_equal(python_argv[8], "STAGE0") &&
          string_equal(python_argv[14], "execute") &&
          string_equal(python_argv[23], "--confirm-execute") &&
          string_equal(python_argv[24], CONFIRMATION) && python_argv[25] == NULL &&
          string_equal(python_environment[0], "LC_CTYPE=C.UTF-8") &&
          python_environment[1] == NULL)) {
        fail();
    }
    syscall1(SYS_EXIT_GROUP, 42);
    fail();
#else
    syscall5(
        SYS_EXECVEAT,
        python_fd,
        (isize)(u64)(const void *)"",
        (isize)(u64)(void *)python_argv,
        (isize)(u64)(void *)python_environment,
        AT_EMPTY_PATH
    );
    fail();
#endif
}

__asm__(
    ".global _start\n"
    ".type _start,@function\n"
    "_start:\n"
    "xor %rbp,%rbp\n"
    "mov %rsp,%rdi\n"
    "and $-16,%rsp\n"
    "call shim_main\n"
    "ud2\n"
    ".size _start,.-_start\n"
);
