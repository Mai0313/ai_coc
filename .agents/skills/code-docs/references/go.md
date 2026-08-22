# Go — Doc Comments

The canonical references are <https://google.github.io/styleguide/go/decisions#doc-comments> and <https://go.dev/doc/comment>. `gofmt`, `go doc`, and pkg.go.dev all assume this exact shape, so straying from it breaks the rendered output.

Examples below show **structure**. They are not a length target; keep real comments to what `SKILL.md` describes.

## What every exported declaration gets

A `//` comment block immediately above a top-level declaration, with no blank line between. Three rules cover most cases:

1. **Start with the identifier name.** "`FetchUser returns …`", not "Returns …". Non-negotiable; `go doc` and pkg.go.dev depend on it.
2. **Full sentences.** Capitalised, ending in a period.
3. **A blank `//` line between paragraphs.**

Go has no `Args:` / `Returns:` sections. The doc is prose. Mention parameters and results inline, and only where the names alone do not make it obvious.

## Worked example — function

```go
// FetchUser returns the User with the given id from the active session.
//
// It returns nil and UserNotFoundError when no such user exists. Any other
// error is transient and the call may be retried. FetchUser is safe for
// concurrent use.
func FetchUser(ctx context.Context, id int64) (*User, error) {
```

The errors are named and the concurrency contract is stated. Neither is visible from the signature, which is why they are the only things worth writing down.

## Type

```go
// Cache stores Users keyed by id.
//
// A zero Cache is not ready for use; construct one with NewCache. Cache is
// safe for concurrent use.
type Cache struct {
```

For a type, state its purpose, its zero-value semantics, the constructor callers must use, and the concurrency contract.

## Method

```go
// Get returns the cached User for id, fetching from upstream on a miss.
// The error is the upstream error verbatim. Callers do not need to lock.
func (c *Cache) Get(ctx context.Context, id int64) (*User, error) {
```

Say what the caller must do, not what the method does internally. "Callers do not need to lock" is useful; walking through which lock is taken when is implementation detail that will rot.

## Package

```go
// Package smalltable provides a typed client for the Smalltable service.
//
// The package is safe for concurrent use; see Dial for connection pooling
// caveats.
package smalltable
```

Package docs live in a single file, conventionally `doc.go`. Lead with `Package <name>`.

## Interface

```go
// Reader reads bytes into an internal buffer and returns them on demand.
//
// Implementations must be safe for concurrent use, and must return io.EOF
// rather than a custom sentinel when the input is exhausted.
type Reader interface {
    // Read returns the next chunk of buffered bytes. The slice is valid
    // until the next call to Read; copy it to retain it.
    Read(ctx context.Context) ([]byte, error)
}
```

Document the interface, because that is the contract. An implementation needs its own doc only where its behaviour deviates from that contract.

## Unexported functions and tests

One line, stating any precondition. That is where the load-bearing complexity lives.

```go
// drainLocked drains pending items from c.queue. The caller must hold c's
// write lock.
func (c *Cache) drainLocked() {
```

```go
// TestCacheGetSkipsUpstreamOnHit verifies that a second Get for the same id
// does not call the fetcher.
func TestCacheGetSkipsUpstreamOnHit(t *testing.T) {
```

## Idioms and pitfalls

- **Doc links use brackets.** Since Go 1.19, `[FetchUser]`, `[Cache.Get]`, and `[pkg.Name]` render as links. Use them to cross-reference instead of writing bare identifiers or spelled-out paths.

- **Lists are supported.** A marker of `*`, `+`, `-`, or `•` followed by a space or tab starts a bullet list, and `gofmt` normalises the rest.

- **Code blocks get one tab.** `gofmt` re-indents every code block in a doc comment to a single tab and puts a blank line either side, so do not hand-align with spaces.

- **Do not restate the signature.** "FetchUser takes a context and an id" is noise.

- **Name the sentinel errors** callers might compare against (`io.EOF`, `os.ErrNotExist`, a package `Err…`), and say so when errors are wrapped.

- **Deprecation** is a paragraph beginning `Deprecated:`. Tools match that exact spelling.

    ```go
    // OldFetch returns a User by id.
    //
    // Deprecated: Use [FetchUser] instead. OldFetch will be removed in v2.
    func OldFetch(id int64) *User {
    ```

- **`func Example…`** becomes a runnable example on pkg.go.dev and runs under `go test`. Never invent one you have not verified compiles.

## What "stale" looks like

- The doc says the function returns nil on not-found; the code returns a sentinel error.
- The doc claims thread safety the code does not deliver.
- The function gained a `ctx` for cancellation and the doc still says it blocks.
- The leading identifier is the function's old name.
- `Deprecated:` sitting on what is still the recommended path.

Rewrite the whole comment to the current contract rather than patching a single sentence.
