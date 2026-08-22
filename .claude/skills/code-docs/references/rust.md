# Rust — Doc Comments

The canonical references are <https://google.github.io/styleguide/rustguide.html> and <https://doc.rust-lang.org/rustdoc/how-to-write-documentation.html>. `cargo doc` parses these comments into HTML, so structure matters.

Use `///` for items and `//!` for a module or crate root. The body is Markdown, and `cargo doc` turns the `#` headings into a structured layout.

Examples below show **structure**. They are not a length target; keep real comments to what `SKILL.md` describes.

## What every public item gets

A doc block immediately above the item, with no blank line between:

1. **Summary line** — one sentence ending in a period.
2. **Extended description** (optional) — paragraphs after a blank `///` line.
3. **Sections** as `# Heading`, only the ones that apply, in this order:
    - `# Examples` — real doctests; `cargo test --doc` compiles and runs them.
    - `# Panics` — what causes a panic.
    - `# Errors` — for `Result`, what each variant means.
    - `# Safety` — for `unsafe fn`, the invariants the caller must uphold.

## Worked example — function

````rust
/// Returns the area of a rectangle.
///
/// # Examples
///
/// ```
/// assert_eq!(geometry::rectangle_area(3.0, 4.0), 12.0);
/// ```
///
/// # Panics
///
/// Panics if either `width` or `height` is negative.
pub fn rectangle_area(width: f64, height: f64) -> f64 {
````

Identifiers and types go in backticks so rustdoc can resolve them. Parameters are named in prose, never in an `Args`-style table.

## Result-returning function

```rust
/// Parses `s` as a URL, trimming surrounding whitespace first.
///
/// # Errors
///
/// - [`ParseError::Empty`] if `s` is empty after trimming.
/// - [`ParseError::Scheme`] if the scheme contains characters outside `[a-z]`.
/// - [`ParseError::Host`] if the authority is malformed.
pub fn parse_url(s: &str) -> Result<Url, ParseError> {
```

Listing the variants tells the caller what to match on, which a single sentence cannot.

## Unsafe function

```rust
/// Reads `len` bytes from `ptr` into a fresh `Vec<u8>`.
///
/// # Safety
///
/// - `ptr` must be valid for reads of `len` bytes.
/// - The memory must not be mutated for the duration of the call.
/// - `len` must not exceed `isize::MAX`.
pub unsafe fn read_bytes(ptr: *const u8, len: usize) -> Vec<u8> {
```

Every `unsafe fn` needs `# Safety`, and its job is to enumerate what the *caller* must guarantee.

## Type and trait

```rust
/// A bounded queue with a fixed capacity.
///
/// Single-producer, single-consumer; any other pattern needs external
/// synchronisation. Construct with [`BoundedQueue::with_capacity`].
pub struct BoundedQueue<T> { /* ... */ }
```

```rust
/// A source of bytes that can be read incrementally.
///
/// Implementations must return `Ok(0)` at end of input, and keep returning
/// it on subsequent reads.
pub trait ByteSource {
    /// Reads up to `buf.len()` bytes into `buf`, returning how many were read.
    ///
    /// # Errors
    ///
    /// Whatever I/O error the implementation surfaces.
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize>;
}
```

A type doc covers what it represents, how to construct it, and its concurrency contract. A trait doc is the contract itself, and each method states what it imposes on implementers.

## Module

```rust
//! Streaming parsers for the HTTP/1.1 wire format.
//!
//! The parsers do not allocate; the caller supplies the buffers.
```

`//!` goes at the top of the file for a module, and in `lib.rs` or `main.rs` for the crate.

## Private items and tests

`pub(crate)`, `pub(super)`, and bare `fn` all count as private for this sweep. One `///` line, stating any precondition:

```rust
/// Drains pending items from the inner queue. Caller must hold the lock.
fn drain_locked(&mut self) {
```

Tests are not rendered by `cargo doc`, so a plain `//` comment is equally fine. If you do use a doc comment, keep it above the attribute:

```rust
/// Verifies that a queue at capacity returns `PushError::Full`.
#[test]
fn push_at_capacity_returns_full() {
```

## Idioms and pitfalls

- **`# Examples` blocks are compiled and run.** Never write one you have not verified. Prefix a line inside the block with `# ` to hide setup from the rendered page while keeping it in the build.
- **Intra-doc links** are written `` [`MyType`] `` or `` [`module::func`] `` and resolve within the crate and its dependencies. Prefer them to spelling out a path in prose.
- **`# Panics` is mandatory** for anything that can panic on valid-looking input. A `Result` API that panics is a contract bug in the code, not something to document away.
- **Deprecation** uses `#[deprecated(since = "...", note = "...")]` rather than a prose paragraph, so the note reaches tooling.
- **Do not reach for `#[doc(hidden)]`** to avoid documenting a public item. Either make it private or document it.

## What "stale" looks like

- `# Errors` listing variants no longer returned, or missing new ones.
- `# Panics` left over from an assertion since removed.
- An example built on an API shape that no longer compiles; `cargo test --doc` catches this when CI runs doctests.
- `# Safety` invariants the implementation no longer relies on.
- A type claiming thread safety with no `Send` / `Sync` impls, or with an internal `Cell`.

Rewrite the whole comment to the current contract rather than patching a single line.
