# C / C++ — Google Style Comments

The canonical reference is <https://google.github.io/styleguide/cppguide.html#Comments>, which the C code here follows too. Google's house style is **prose**: no `@param`, `@return`, or other Doxygen tags. Write what an experienced caller needs to know, immediately above the declaration.

Examples below show **structure**. They are not a length target; keep real comments to what `SKILL.md` describes.

## What every public declaration gets

A `//` block immediately above the declaration, with no blank line between, covering:

1. What it does, in a sentence or two.
2. The contracts a caller cannot see: preconditions, ownership, lifetime, thread safety, side effects.
3. What the return value and any out-parameters mean, including on failure.

Do not force an `Args:` / `Returns:` shape onto C or C++. The style is deliberately freeform.

## Worked example — function

```cpp
// Returns an iterator positioned at the first row of `table`. The caller
// owns the iterator, which must not outlive `table`.
std::unique_ptr<TableIterator> NewIterator(const Table& table);
```

Ownership and lifetime are the whole point of the comment. Prefer `std::unique_ptr` in new code; when maintaining an API that already returns a raw owning pointer, say plainly that the caller must `delete` it.

## Out-parameters

```cpp
// Writes `data` to `path`, creating the file or truncating it.
//
// On success sets `*bytes_written` and returns true. On failure sets it to 0,
// returns false, and leaves the reason in `errno`. `bytes_written` must not
// be null.
bool WriteFile(std::string_view path, std::string_view data,
               int* bytes_written);
```

Out-parameters need the failure case spelled out, because that is where callers get it wrong.

## Class and method

```cpp
// A bounded queue with fixed capacity, for one producer and one consumer.
//
// Construct with a capacity; a default-constructed queue is unusable.
// Thread-compatible: Push and Pop may run concurrently, but only one thread
// may call each.
class BoundedQueue {
 public:
  // Returns the cached User for `id`, or nullptr if absent. The pointer is
  // owned by the queue and is invalidated by Insert, Clear, or destruction.
  const User* Get(int64_t id) const;
};
```

The class comment carries purpose, construction, and the concurrency contract. Each public method then carries its own.

## Pure C

The same style, with C idioms:

```c
// Reads the next record from `f` into `out`.
//
// Returns 0 on success with `*out` populated, 1 at end of file with `*out`
// untouched, and -1 on error with `errno` set and `*out` unspecified.
// `f` and `out` must be non-null.
int read_record(FILE* f, struct Record* out);
```

## Headers, static functions, and tests

A header opens with a comment naming what it declares and any stability guarantee. A `static` function in a `.c` or `.cc` gets one line, plus a precondition when one is load-bearing. A test gets one line naming the behaviour verified.

```c
// Lowercases all ASCII letters in `s` in place. `s` must be NUL-terminated.
static void lowercase_ascii(char* s);
```

```cpp
// Verifies that pushing onto a full queue returns kFull.
TEST(BoundedQueueTest, PushAtCapacityReturnsFull) {
```

## Idioms and pitfalls

- **Ownership is the most valuable sentence** in any pointer-returning API. "Caller takes ownership", "owned by `this`", and "valid until the next call to X" each carry more weight than a paragraph of description.
- **State thread safety** on anything shared across threads: "thread-safe", "not thread-safe", or "thread-compatible" (const methods safe, non-const need external synchronisation).
- **Macros** used like functions are documented like functions. Document the expansion only when the call shape is not obvious.
- **Templates**: document the primary declaration. A specialisation needs its own comment only where it diverges.
- **Conditional compilation**: if a declaration exists only under an `#ifdef`, name the platforms or feature gates.
- **`extern "C"`**: comment why C linkage is needed, since that is never obvious from the block itself.

## What "stale" looks like

- An ownership claim the implementation contradicts, in either direction.
- A thread-safety claim with no locks behind it, or locks added and the comment untouched.
- An out-parameter contract saying "set on failure" when the code only writes on success.
- A precondition that a later `nullptr` check made optional.
- An example on an API shape that no longer compiles.

Rewrite the whole block to the current contract. Half-old, half-new is worse than nothing, because it signals false confidence.

## Doxygen-flavoured projects

If the project already uses `@param` / `@return`, that may be a deliberate choice, for instance a library that ships HTML docs. Raise it rather than switching silently. Once the user confirms Google Style, convert wholesale: a consistent style beats perfect per-function tagging.
