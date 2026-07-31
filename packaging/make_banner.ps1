# Part of kramer-vs44-remote-control. Copyright (C) 2026 Piero Biagini.
# Licensed under the GNU GPL v3 or later. See LICENSE for details.
#
# Generate the README banner: the application mark with the project name beside
# it, as two SVGs - one for each GitHub theme.
#
#   pwsh packaging/make_banner.ps1
#
# Both SVGs are committed, so nothing has to run this. It exists so the artwork
# has a source rather than being a blob of unknown provenance, exactly like
# make_icon.py next to it.
#
# Why the lettering is converted to outlines rather than left as <text>: GitHub
# renders a README SVG as an image, so any font-family resolves against whatever
# the *reader* has installed. The wordmark would change shape from one machine to
# the next, and a lockup that is centred on one would be off-centre on another.
# Outlines render identically everywhere and need no font at all.
#
# Two things this script is not:
#
#   - Portable. It uses System.Drawing to turn glyphs into Bezier paths, which
#     is Windows-only. Regenerating on Linux would mean a different tool; the
#     committed SVGs mean nobody has to.
#   - Self-contained. It needs Lato, which is NOT committed here: shipping a
#     font is redistributing font software, with its own licence obligations,
#     and this project has no need to. The outlines it produces are artwork.
#
# Lato is used because it is openly licensed (SIL Open Font License 1.1) and the
# fonts that ship with Windows - Segoe UI, Arial, Verdana - are not. Baking a
# proprietary typeface's outlines into a GPLv3 repository is a licensing problem
# that is easy to create by accident and tedious to undo.
#
# Fetch the two files before running, into the directory given by -FontDir:
#   https://github.com/google/fonts/tree/main/ofl/lato  ->  Lato-Bold.ttf,
#                                                           Lato-Regular.ttf

param(
    [string]$FontDir = ".",
    [string]$OutDir  = $PSScriptRoot
)

Add-Type -AssemblyName System.Drawing

# Every number below is formatted with -f, which follows the current culture. On
# an Italian or German machine that means a decimal COMMA, and in SVG path data a
# comma separates coordinates: "M1,5 2,7" silently becomes four numbers instead
# of two and the artwork collapses into a sliver. Pin the culture rather than
# remembering to pass InvariantCulture at a dozen call sites.
[Threading.Thread]::CurrentThread.CurrentCulture =
    [Globalization.CultureInfo]::InvariantCulture

# --- the mark, in the same 32-unit geometry as make_icon.py and the web page --
$ACCENT     = "#1F7A4D"
$MARK       = "#FFFFFF"
$ICON       = 64.0            # rendered size of the square, in SVG units
$GAP        = 20.0            # between the mark and the lettering
$PAD        = 2.0             # trailing space, so nothing touches the edge
$LINE_GAP   = 7.0             # between the two lines of lettering
$NAME_SIZE  = 30.0
$SUB_SIZE   = 16.0

# One theme each. The mark keeps its colour in both: it is the brand, and a
# solid green square reads perfectly well on either background.
$THEMES = @(
    @{ File = "banner-light.svg"; Name = "#1F2328"; Sub = "#59636E" }
    @{ File = "banner-dark.svg";  Name = "#E6EDF3"; Sub = "#8B949E" }
)

function Get-TextPath {
    <#
      A string as an SVG path, plus its measured bounds.

      GenericTypographic rather than the default StringFormat: the default adds
      padding around the string that would silently offset the layout.
    #>
    param([string]$Text, [System.Drawing.FontFamily]$Family,
          [System.Drawing.FontStyle]$Style, [double]$Size)

    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddString($Text, $Family, [int]$Style, $Size,
                    (New-Object System.Drawing.PointF(0, 0)),
                    [System.Drawing.StringFormat]::GenericTypographic)

    $pts = $path.PathPoints
    $types = $path.PathTypes
    $sb = New-Object System.Text.StringBuilder
    $n = ($(if ($pts) { $pts.Count } else { 0 }))

    $i = 0
    while ($i -lt $n) {
        # The low three bits are the segment kind; 0x80 on a point means "and
        # close this figure here".
        $kind = $types[$i] -band 0x07
        switch ($kind) {
            0 { [void]$sb.Append(("M{0:0.##} {1:0.##}" -f $pts[$i].X, $pts[$i].Y))
                $last = $i; $i++ }
            1 { [void]$sb.Append(("L{0:0.##} {1:0.##}" -f $pts[$i].X, $pts[$i].Y))
                $last = $i; $i++ }
            3 { [void]$sb.Append(("C{0:0.##} {1:0.##} {2:0.##} {3:0.##} {4:0.##} {5:0.##}" -f `
                    $pts[$i].X,   $pts[$i].Y,
                    $pts[$i+1].X, $pts[$i+1].Y,
                    $pts[$i+2].X, $pts[$i+2].Y))
                $last = $i + 2; $i += 3 }
            default { $i++ ; continue }
        }
        if (($types[$last] -band 0x80) -ne 0) { [void]$sb.Append("Z") }
    }

    $b = $path.GetBounds()
    $path.Dispose()
    [pscustomobject]@{
        D = $sb.ToString()
        X = [double]$b.X; Y = [double]$b.Y
        W = [double]$b.Width; H = [double]$b.Height
    }
}

# --- load the fonts without installing them ---------------------------------
$coll = New-Object System.Drawing.Text.PrivateFontCollection
foreach ($f in "Lato-Bold.ttf", "Lato-Regular.ttf") {
    $p = Join-Path $FontDir $f
    if (-not (Test-Path $p)) { throw "missing $p - see the header of this script" }
    $coll.AddFontFile((Resolve-Path $p).Path)
}
$lato = $coll.Families[0]

$name = Get-TextPath -Text "Kramer VS-44HN" -Family $lato `
                     -Style ([System.Drawing.FontStyle]::Bold) -Size $NAME_SIZE
$sub  = Get-TextPath -Text "Remote Control" -Family $lato `
                     -Style ([System.Drawing.FontStyle]::Regular) -Size $SUB_SIZE

# --- lay it out --------------------------------------------------------------
# Measured bounds, not font metrics: what matters is where the ink actually is,
# so the block is optically centred against the mark rather than mathematically
# centred against an em box that includes space for descenders nothing uses.
$textLeft  = $ICON + $GAP
$blockH    = $name.H + $LINE_GAP + $sub.H
$blockTop  = ($ICON - $blockH) / 2.0
$width     = [math]::Ceiling($textLeft + [math]::Max($name.W, $sub.W) + $PAD)
$height    = $ICON

# Translate each run so its ink starts exactly where the layout says.
$nameDx = $textLeft - $name.X
$nameDy = $blockTop - $name.Y
$subDx  = $textLeft - $sub.X
$subDy  = $blockTop + $name.H + $LINE_GAP - $sub.Y

foreach ($t in $THEMES) {
    $svg = @"
<svg xmlns="http://www.w3.org/2000/svg" width="$width" height="$height" viewBox="0 0 $width $height" role="img" aria-label="Kramer VS-44HN Remote Control">
  <!-- Generated by packaging/make_banner.ps1 - do not edit by hand.
       Lettering: outlines derived from Lato (SIL Open Font License 1.1),
       Copyright (c) 2010-2014 tyPoland Lukasz Dziedzic. -->
  <g transform="scale($($ICON / 32.0))">
    <rect x="0" y="0" width="32" height="32" rx="6" fill="$ACCENT"/>
    <rect x="7"  y="7"  width="8" height="8" rx="2" fill="$MARK"/>
    <rect x="17" y="7"  width="8" height="8" rx="2" fill="$MARK"/>
    <rect x="7"  y="17" width="8" height="8" rx="2" fill="$MARK"/>
    <rect x="17" y="17" width="8" height="8" rx="2" fill="$MARK"/>
  </g>
  <path transform="translate($('{0:0.##}' -f $nameDx) $('{0:0.##}' -f $nameDy))" fill="$($t.Name)" d="$($name.D)"/>
  <path transform="translate($('{0:0.##}' -f $subDx) $('{0:0.##}' -f $subDy))" fill="$($t.Sub)" d="$($sub.D)"/>
</svg>
"@
    $out = Join-Path $OutDir $t.File
    # UTF-8 without a BOM: an SVG that starts with one is still valid, but the
    # project has been bitten by a BOM once already.
    [IO.File]::WriteAllText($out, $svg, (New-Object Text.UTF8Encoding($false)))
    "wrote $out  ($([math]::Round((Get-Item $out).Length / 1KB, 1)) KB, ${width}x${height})"
}
