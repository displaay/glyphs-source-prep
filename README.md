# glyphs-source-prep

Fix the things **Glyphs.app tolerates** in a `.glyphs` source and **glyphsLib
does not**.

A source that opens, edits and exports perfectly well in Glyphs can fail the
`fontmake` build — sometimes deep inside varLib, with a message that says
nothing about what is actually wrong with the file. This package repairs those
states before the builder sees them.

Every fix here changes what the *editor* recorded, never what the font will
look like.

## Installation

Not on PyPI on purpose: this package is meant to be deleted once upstream catches up, and a PyPI name cannot be freed again once taken. Releases are wheels attached to a GitHub Release, so pin the asset URL:

```
glyphs-source-prep @ https://github.com/displaay/glyphs-source-prep/releases/download/v0.1.0/glyphs_source_prep-0.1.0-py3-none-any.whl
```

pip fetches a built wheel over plain HTTPS - no git needed in the build image, and it caches like any other wheel. For local work:

```bash
pip install -e .
```

`glyphsLib` is deliberately **not** a runtime dependency: the `GSFont`-based
fixes duck-type what they touch and import glyphsLib only for type checking, so
this package adds nothing to an environment that already has one and can sit
next to any glyphsLib version.

## Usage

```python
import glyphsLib
from glyphs_source_prep import (
    align_brace_layers_to_variable_origin,
    drop_dangling_background_components,
)

font = glyphsLib.load("Font.glyphs")

report = drop_dangling_background_components(font)
if report.dropped:
    log.warning(report.summary())
    # 981 background component(s) referencing a non-existent glyph removed
    # from 327 glyph(s): A.old (981)

report = align_brace_layers_to_variable_origin(font)
if report.moved:
    log.info(report.summary())
```

and after the designspace has been generated:

```python
from glyphs_source_prep import deduplicate_designspace_sources

report = deduplicate_designspace_sources("master_ufo/Font.designspace")
```

Every entry point returns a small result object with a `summary()` for a
processing log rather than mutating silently: a source the build had to repair
is something its author should hear about.

## What it fixes

### Dangling background components

Glyphs.app is happy for a background layer to reference a glyph that was later
renamed or deleted — the background is a sketch pad, it is never compiled.
glyphsLib decomposes background components like any others and raises
`MissingComponentError`, so a whole build fails over drawing scaffolding.

`drop_dangling_background_components(font)` removes only the dangling
component — never the layer, and never outside a background. A dangling
component in a *drawing* layer changes the output and must keep failing.

> Upstream: [glyphsLib#743](https://github.com/googlefonts/glyphsLib/issues/743),
> open since 2021. The maintainers are receptive — *"one can make a case for
> skipping such components by default or under an option"* — and fontTools now
> ships the mechanism (`DecomposingRecordingPen(..., skipMissingComponents=True)`),
> but glyphsLib does not pass it yet.

### Brace layers hung off different masters

Glyphs.app treats which master an intermediate layer is attached to as an
editing convenience. glyphsLib turns each association into a separate
designspace source, so two glyphs that hang the same location off different
masters produce duplicate locations and varLib fails with *"Locations must be
unique"*.

Georg Seifert, in
[glyphsLib#925](https://github.com/googlefonts/glyphsLib/issues/925):

> A brace layer is what is called a sparse master in ufo/designspace. So no
> connection to the master it is attached to. It just has to be somewhere.

`align_brace_layers_to_variable_origin(font)` reassigns the layers that are
actually in conflict to the variable font origin master.

**Only the ones in conflict.** A source whose brace layers are spread across
several masters is perfectly healthy as long as each *location* belongs to one
master. Measured across eight retail families, six had brace layers on several
masters and none had a conflicting location — reassigning them all, as a
Glyphs user does by hand, would have rewritten up to a hundred layers per
family for nothing. Pass `only_conflicts=False` for that behaviour.

**And "the same location" is not the coordinates in the layer name.**
glyphsLib fills a short coordinate list up from the associated master, so on a
wght/wdth font a `{80}` layer on a Width 100 master and a `{80}` layer on a
Width 50 master are two *different* sources, while `{80, 100}` on one master
collides with `{80}` on a master whose Width is 100. Comparing raw
coordinates gets both cases wrong.

That padding also limits the repair: for a layer whose coordinates are short,
the associated master is what supplies the rest of the location, so moving it
would shift the sparse master and change what the font interpolates. Those are
reported in `unresolved_glyphs` for a human to resolve, never moved.

> Upstream: [glyphsLib#995](https://github.com/googlefonts/glyphsLib/issues/995),
> open since 2024. *"ideally we should [ignore the putative master for an
> intermediate layer]. PR most welcome."* — and from the Glyphs author, *"If
> someone could put that into a test case, I could fix this in the Glyphs3
> branch."*

### Master contours stored in a different order

A glyph's contours are positional: nothing in the source says which contour of
one master corresponds to which of another. Two masters holding the same
contours in a different order therefore describe a glyph that cannot be
interpolated, and ufo2ft fails the build with *"glyph 'one.tf' has incompatible
masters"* — seen on a retail source whose proportional masters stored `(8, 4)`
point counts and whose mono masters stored `(4, 8)`.

`reorder_master_paths_to_variable_origin(font)` puts every master's contours in
the origin's order. Reordering cannot change how a master renders — a glyph is
filled from all its contours at once, and each keeps its own direction and
points — only which contour pairs with which across masters.

It acts **only when the pairing is unambiguous.** The correspondence is inferred
from point counts, so a glyph whose counts contain a duplicate — a colon with
two four-point dots, a dieresis, a quotation mark — offers no evidence about
which contour belongs where. Guessing would silently pair the wrong contours and
change what every intermediate instance draws, so those land in
`ambiguous_glyphs` for a human instead. A build that fails is better than a font
that interpolates through the wrong shape.

> No upstream issue: unlike the other fixes here this is not a glyphsLib bug but
> a source state, and the compatibility check that rejects it is correct to do
> so. It is in this package because it is the same kind of repair — what the
> editor recorded, not what the font looks like — and because the Builder and
> the Customizer both need it.

### Duplicate designspace sources

The last line of defence behind the brace layer alignment, for a designspace
that was not built by glyphsLib or for a case the alignment does not reach.

`deduplicate_designspace_sources(path)` drops sources that share an axis
location, keeping the full master over a sparse layer source — dropping the
full one would take a real master out of the design space. The file is
rewritten only when something was actually removed.

> Upstream: [glyphsLib#925](https://github.com/googlefonts/glyphsLib/issues/925)
> fixed the layer-naming half of this in 6.2.4/6.2.5. What remains is #995.

### A source that is not valid UTF-8

Glyphs.app has written UTF-8 for years, but a source that started life in an
older version keeps whatever single bytes were typed into it then — a
designer's name, a copyright line — as MacRoman. The editor still opens it; it
falls back per byte. Python does not, and `data.decode("utf-8")` raises before
glyphsLib is ever reached, pointing at a byte offset rather than a string.

```python
from glyphs_source_prep import decode_source_text

result = decode_source_text(raw)        # bytes, or an already-decoded str
if result.rewritten:
    log.warning(result.summary())
font = GSFont(result.text)
```

Files like this are almost always *mixed*: mostly UTF-8, with a few legacy
bytes in the middle. Decoding the whole file as MacRoman would therefore be
wrong — every genuine multi-byte character would become mojibake — so this
decodes as UTF-8 as far as it can, takes the one offending byte as MacRoman,
and resumes.

> Not a glyphsLib bug and no upstream issue: by the time glyphsLib sees a
> source it is already `str`. It lives here because it is the same class of
> problem one step earlier, and both tools need it at the same point.

## When to delete each fix

Every module names the upstream issue it works around and what would close it.
These are all bugs the maintainers have agreed should be fixed in glyphsLib —
none is a permanent difference of opinion — so check the issues before assuming
a fix is still needed, and drop the ones that have landed. The exception is
`encoding`, which works around no bug: it stays as long as sources written by
older Glyphs versions do.

The tests are the useful part to keep either way: they are small, they assert
the *behaviour* rather than the workaround, and they double as the reproduction
cases those issues have been waiting for.

## A note on private API

`align_brace_layers_to_variable_origin` has to ask whether a layer is a brace
layer, and `GSLayer._is_brace_layer()` is private: it reads `self.attributes` on
Glyphs 3 and parses the layer name on Glyphs 2, both through
`self.parent.parent`. It is therefore called defensively — a layer that cannot
be classified is left alone rather than taking the build down — and CI runs the
test matrix across the supported glyphsLib range so a change in shape shows up
here rather than in a user's build.

## Related

* [glyphs4to3](https://github.com/displaay/glyphs4to3) — read a Glyphs 4
  (`.formatVersion = 4`) source with a parser that only knows Glyphs 3. That
  one is about the file *format*; this one is about the file *contents*.

## Licence

MIT
