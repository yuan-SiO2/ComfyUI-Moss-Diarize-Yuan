/* 极简 bump 分配器：只给 worklet 的临时缓冲用。
 *
 * Sonic 自身用 -DSONIC_NO_MALLOC 编译，内部有静态 arena，不依赖这里的 malloc。
 * 所以这里只需要：分配一次、永不释放（worklet 在初始化时分配两块 scratch），
 * 内存不够就向 wasm 线性内存要新页。
 */
#include <stddef.h>
#include <stdint.h>

extern unsigned char __heap_base;

void *wasm_mem_grow(int32_t pages) {
	size_t old = __builtin_wasm_memory_size(0);
	if (__builtin_wasm_memory_grow(0, pages) == (size_t)-1) return 0;
	return (void *)(old * 65536);
}

static unsigned char *heap = 0;
static unsigned char *heap_end = 0;
static size_t heap_size = 0;

static void heap_init(void) {
	if (heap) return;
	heap = (unsigned char *)&__heap_base;
	heap_end = (unsigned char *)(size_t)(__builtin_wasm_memory_size(0) * 65536);
	heap_size = (size_t)(heap_end - heap);
}

/* 保留给外部调用的重置入口（当前流程不使用） */
void arena_reset(void) {
	heap = (unsigned char *)&__heap_base;
}

void *memcpy(void *dst, const void *src, size_t n) {
	unsigned char *d = dst;
	const unsigned char *s = src;
	while (n--) *d++ = *s++;
	return dst;
}

void *memmove(void *dst, const void *src, size_t n) {
	unsigned char *d = dst;
	const unsigned char *s = src;
	if (d == s || n == 0) return dst;
	if (d < s) {
		while (n--) *d++ = *s++;
	} else {
		d += n;
		s += n;
		while (n--) *--d = *--s;
	}
	return dst;
}

void *memset(void *dst, int c, size_t n) {
	unsigned char *d = dst;
	while (n--) *d++ = (unsigned char)c;
	return dst;
}

void *malloc(size_t size) {
	heap_init();
	if (size == 0) return 0;
	size_t total = ((size + 15) & ~(size_t)15) + 16;
	if (heap + total > heap_end) {
		size_t need = (size_t)(heap + total - heap_end);
		int pages = (int)((need + 65535) / 65536);
		if (!wasm_mem_grow(pages)) return 0;
		heap_end = (unsigned char *)(size_t)(__builtin_wasm_memory_size(0) * 65536);
	}
	size_t *hdr = (size_t *)heap;
	hdr[0] = size;
	heap += total;
	return (void *)(hdr + 2);
}

void free(void *ptr) {
	(void)ptr;
}

void *calloc(size_t nmemb, size_t size) {
	size_t total = nmemb * size;
	void *p = malloc(total);
	if (p) memset(p, 0, total);
	return p;
}

void *realloc(void *ptr, size_t size) {
	if (!ptr) return malloc(size);
	size_t *hdr = (size_t *)((unsigned char *)ptr - 16);
	size_t old = hdr[0];
	if (old >= size) return ptr;
	void *fresh = malloc(size);
	if (!fresh) return 0;
	memcpy(fresh, ptr, old);
	return fresh;
}
