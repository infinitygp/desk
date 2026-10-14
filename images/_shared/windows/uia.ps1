# UI Automation side of `desk a11y` on Windows: the same JSON protocol as desk/guest_a11y.py
# (snapshot, resolve, action, set-text, scroll). UIA reports screen pixels directly; the test
# images run at 100 % scaling so they match the screenshots.
$AE = [Windows.Automation.AutomationElement]
$ControlView = [Windows.Automation.Automation]::ControlViewCondition
$Walker = [Windows.Automation.TreeWalker]::ControlViewWalker
$RawWalker = [Windows.Automation.TreeWalker]::RawViewWalker

$Roles = @{
    'Button' = 'button'; 'SplitButton' = 'button'; 'CheckBox' = 'check box'; 'RadioButton' = 'radio button'
    'ComboBox' = 'combo box'; 'Edit' = 'entry'; 'Document' = 'document'; 'Hyperlink' = 'link'
    'ListItem' = 'list item'; 'List' = 'list'; 'MenuItem' = 'menu item'; 'Menu' = 'menu'; 'MenuBar' = 'menu bar'
    'TabItem' = 'page tab'; 'Tab' = 'page tab list'; 'TreeItem' = 'tree item'; 'Tree' = 'tree'
    'DataItem' = 'table cell'; 'Table' = 'table'; 'DataGrid' = 'table'; 'HeaderItem' = 'column header'
    'Slider' = 'slider'; 'Spinner' = 'spin button'; 'Text' = 'label'; 'Image' = 'image'; 'Window' = 'window'
    'Pane' = 'panel'; 'Group' = 'grouping'; 'ToolBar' = 'tool bar'; 'StatusBar' = 'status bar'
    'TitleBar' = 'title bar'; 'ScrollBar' = 'scroll bar'; 'ProgressBar' = 'progress bar'; 'Custom' = 'unknown'
    'Header' = 'header'; 'Separator' = 'separator'; 'Thumb' = 'thumb'; 'Calendar' = 'calendar'
    'ToolTip' = 'tool tip'; 'SemanticZoom' = 'panel'; 'AppBar' = 'tool bar'
}
$Interactive = 'button', 'check box', 'radio button', 'combo box', 'entry', 'link', 'list item', 'menu item',
    'page tab', 'tree item', 'table cell', 'slider', 'spin button', 'menu'
$Quiet = 'panel', 'grouping', 'unknown', 'title bar', 'scroll bar', 'thumb', 'separator', 'header'
$MaxVisit = 6000
# A walk also ends by the clock, as on Linux guests: an application that answers slowly would
# otherwise keep the agent, which serves one request at a time, past the host's patience.
$WalkSeconds = 20

# Some classic list views (the desktop's icons: FolderView, a SysListView32 in Explorer) show
# up in the managed UIA client as an empty pane; their items are only reachable through MSAA.
Add-Type -AssemblyName Accessibility
Add-Type -ReferencedAssemblies Accessibility -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
namespace Desk {
    public class MsaaItem { public int Child; public string Name; public int X, Y, W, H, State; }
    public static class Msaa {
        [DllImport("oleacc.dll")]
        static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint id, ref Guid iid,
            [MarshalAs(UnmanagedType.Interface)] out object acc);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        static extern IntPtr FindWindowEx(IntPtr parent, IntPtr after, string cls, string name);
        [DllImport("user32.dll")]
        static extern bool IsWindowVisible(IntPtr hwnd);
        // Open Win32 menus (popups of class #32768), which UI Automation's managed client misses.
        public static List<IntPtr> Menus() {
            var menus = new List<IntPtr>();
            IntPtr h = IntPtr.Zero;
            while ((h = FindWindowEx(IntPtr.Zero, h, "#32768", null)) != IntPtr.Zero)
                if (IsWindowVisible(h)) menus.Add(h);
            return menus;
        }
        public static List<MsaaItem> Items(IntPtr hwnd) {
            var items = new List<MsaaItem>();
            Guid iid = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71");
            object o;
            if (AccessibleObjectFromWindow(hwnd, 0xFFFFFFFC, ref iid, out o) != 0) return items;
            var acc = (Accessibility.IAccessible)o;
            int n = acc.accChildCount;
            for (int i = 1; i <= n; i++) {
                var it = new MsaaItem { Child = i };
                try { it.Name = acc.get_accName(i); } catch { }
                try { int x, y, w, h; acc.accLocation(out x, out y, out w, out h, i); it.X = x; it.Y = y; it.W = w; it.H = h; } catch { }
                try { it.State = Convert.ToInt32(acc.get_accState(i)); } catch { }
                items.Add(it);
            }
            return items;
        }
    }
}
'@

function Get-MsaaNodes($hwnd, $path, $depth, $pid_, $role = 'list item') {
    foreach ($it in [Desk.Msaa]::Items([IntPtr][long]$hwnd)) {
        $states = @()
        if ($it.State -band 0x1) { $states += 'disabled' }   # STATE_SYSTEM_UNAVAILABLE
        if ($it.State -band 0x2) { $states += 'selected' }   # STATE_SYSTEM_SELECTED
        if ($it.State -band 0x4) { $states += 'focused' }    # STATE_SYSTEM_FOCUSED
        if ($it.State -band 0x80) { $states += 'selected' }  # STATE_SYSTEM_HOTTRACKED (menus)
        # Menu items carry their shortcut after a tab ("Close<Tab>Alt+F4").
        $node = [ordered]@{ pid = $pid_; path = ($path + (100000 + $it.Child)); role = $role
                            name = (([string]$it.Name) -split "`t")[0].Trim(); depth = $depth; states = $states
                            bbox = if ($it.W -gt 0 -and $it.H -gt 0) { @($it.X, $it.Y, $it.W, $it.H) } else { $null } }
        if ($it.State -band 0x10000) { $node.offscreen = $true }  # STATE_SYSTEM_OFFSCREEN
        $node
    }
}

function Get-Role($el) {
    $name = $el.Cached.ControlType.ProgrammaticName -replace '^ControlType\.', ''
    if ($name -eq 'Pane') {
        # Classic Win32 dialogs (NSIS installers) come through MSAA with their controls as panes;
        # the window class still tells what they are.
        $class = [string](Cached $el $AE::ClassNameProperty)
        if ($class -eq 'Button' -or $class -eq 'CCPushButton' -or (Cached $el $AE::IsInvokePatternAvailableProperty)) { return 'button' }
        if ($class -eq 'Edit') { return 'entry' }
        if ($class -eq 'Static') { return 'label' }
    }
    if ($Roles.ContainsKey($name)) { $Roles[$name] } else { $name.ToLower() }
}

function New-Cache {
    $cr = New-Object Windows.Automation.CacheRequest
    $cr.TreeScope = [Windows.Automation.TreeScope]::Subtree
    $cr.TreeFilter = $ControlView
    foreach ($p in 'NameProperty', 'ControlTypeProperty', 'BoundingRectangleProperty', 'IsOffscreenProperty',
                   'IsEnabledProperty', 'HasKeyboardFocusProperty', 'ProcessIdProperty', 'NativeWindowHandleProperty',
                   'IsInvokePatternAvailableProperty', 'ClassNameProperty',
                   'IsValuePatternAvailableProperty', 'IsTogglePatternAvailableProperty',
                   'IsSelectionItemPatternAvailableProperty', 'IsExpandCollapsePatternAvailableProperty') {
        $cr.Add($AE::$p)
    }
    $cr.Add([Windows.Automation.ValuePattern]::ValueProperty)
    $cr.Add([Windows.Automation.ValuePattern]::IsReadOnlyProperty)
    $cr.Add([Windows.Automation.TogglePattern]::ToggleStateProperty)
    $cr.Add([Windows.Automation.SelectionItemPattern]::IsSelectedProperty)
    $cr.Add([Windows.Automation.ExpandCollapsePattern]::ExpandCollapseStateProperty)
    $cr
}

function New-ElementCache {
    $cr = New-Cache
    $cr.TreeScope = [Windows.Automation.TreeScope]::Element
    $cr
}

# Classic Win32 dialogs (NSIS and other installers) come through the MSAA proxy, which hands
# back no descendants for a cached subtree. Walk those live, caching one element at a time.
function Walk-Live($live, $path, $depth, $parentName, $hwnd, $nodes, $limit, $crEl) {
    if ($script:truncated -eq 'time') { return }
    if ($script:walkClock.Elapsed.TotalSeconds -gt $WalkSeconds) { $script:truncated = 'time'; return }
    if ($script:visited -ge $MaxVisit -or ($nodes.Count - $script:offscreen) -ge $limit) { $script:truncated = $true; return }
    try { $el = $live.GetUpdatedCache($crEl) } catch { return }
    $script:visited++
    $node = Describe $el $path $depth $hwnd
    $node.live = $true
    $emit = Wanted $node $parentName
    if ($emit -and $node.offscreen) { $emit = $script:offscreen -lt 300; if ($emit) { $script:offscreen++ } }
    if ($emit) { [void]$nodes.Add($node) }
    $childDepth = if ($emit) { $depth + 1 } else { $depth }
    $pname = if ($emit) { $node.name } else { $parentName }
    $i = 0
    $ch = $RawWalker.GetFirstChild($live)
    while ($null -ne $ch) {
        Walk-Live $ch ($path + $i) $childDepth $pname $hwnd $nodes $limit $crEl
        $i++
        $ch = $RawWalker.GetNextSibling($ch)
    }
}

function Cached($el, $prop) {
    $v = $el.GetCachedPropertyValue($prop, $true)
    if ($v -eq [Windows.Automation.AutomationElement]::NotSupported) { $null } else { $v }
}

function Describe($el, $path, $depth, $hwnd) {
    $role = Get-Role $el
    $r = $el.Cached.BoundingRectangle
    $bbox = if ($r.IsEmpty -or $r.Width -le 0 -or $r.Height -le 0) { $null }
            else { @([int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height) }
    $states = @()
    if ($el.Cached.HasKeyboardFocus) { $states += 'focused' }
    if (-not $el.Cached.IsEnabled) { $states += 'disabled' }
    if (Cached $el $AE::IsTogglePatternAvailableProperty) {
        if ((Cached $el ([Windows.Automation.TogglePattern]::ToggleStateProperty)) -eq 'On') { $states += 'checked' }
    }
    if (Cached $el $AE::IsSelectionItemPatternAvailableProperty) {
        if (Cached $el ([Windows.Automation.SelectionItemPattern]::IsSelectedProperty)) { $states += 'selected' }
    }
    if (Cached $el $AE::IsExpandCollapsePatternAvailableProperty) {
        if ((Cached $el ([Windows.Automation.ExpandCollapsePattern]::ExpandCollapseStateProperty)) -eq 'Expanded') {
            $states += 'expanded'
        }
    }
    $node = [ordered]@{ pid = $hwnd; path = $path; role = $role; name = ([string]$el.Cached.Name).Trim()
                        depth = $depth; bbox = $bbox; states = $states }
    if (Cached $el $AE::IsValuePatternAvailableProperty) {
        $v = [string](Cached $el ([Windows.Automation.ValuePattern]::ValueProperty))
        $ro = Cached $el ([Windows.Automation.ValuePattern]::IsReadOnlyProperty)
        if (-not $ro) { $node.states += 'editable' }
        if (-not $ro -or ($v -and $v -ne $node.name)) { $node.value = $v.Substring(0, [Math]::Min(120, $v.Length)) }
    }
    if (-not $node.Contains('value') -and ($role -eq 'document' -or $role -eq 'entry')) {
        # Rich editors (Notepad, Word) expose their text through TextPattern only.
        try {
            $t = $el.GetCurrentPattern([Windows.Automation.TextPattern]::Pattern).DocumentRange.GetText(160)
            $node.value = $t -replace "`r", ''
        } catch { }
    }
    if ($el.Cached.IsOffscreen) { $node.offscreen = $true }
    $node
}

function Wanted($node, $parentName, $leaf = $false) {
    if ($null -eq $node.bbox) { return $false }
    if ($Interactive -contains $node.role) { return $true }
    # A named pane without children is static text in an MSAA-proxied dialog.
    if ($node.role -eq 'panel' -and $leaf -and $node.name) { return $true }
    if ($Quiet -contains $node.role) { return $false }
    if ($node.role -eq 'label' -and $node.name -eq $parentName) { return $false }
    [bool]$node.name
}

function Get-TopWindows {
    # Not filtered on IsOffscreen: Notepad reports its window off-screen while a teaching tip is
    # open. Minimized windows have no rectangle; visibility of content is judged per element.
    @($AE::RootElement.FindAll([Windows.Automation.TreeScope]::Children, $ControlView) |
      Where-Object { try { -not $_.Current.BoundingRectangle.IsEmpty } catch { $true } })
}

function Get-TopOf($el) {
    $root = $AE::RootElement
    $cur = $el
    while ($true) {
        $parent = $Walker.GetParent($cur)
        if ($null -eq $parent -or [Windows.Automation.Automation]::Compare($parent, $root)) { return $cur }
        $cur = $parent
    }
}

function Snapshot($opts) {
    $focused = try { Get-TopOf $AE::FocusedElement } catch { $null }
    $focusHwnd = if ($focused) { $focused.Current.NativeWindowHandle } else { 0 }
    $limit = if ($opts.max) { [int]$opts.max } else { 300 }
    $nodes = New-Object System.Collections.ArrayList
    $tops = New-Object System.Collections.ArrayList
    $script:visited = 0; $script:offscreen = 0; $script:truncated = $false
    $script:walkClock = [Diagnostics.Stopwatch]::StartNew()
    $cr = New-Cache
    foreach ($top in (Get-TopWindows)) {
        try {
            $hwnd = $top.Current.NativeWindowHandle
            $procId = $top.Current.ProcessId
            $title = $top.Current.Name
        } catch {
            [void]$tops.Add([ordered]@{ app = '?'; pid = 0; title = ''; focus = $false; start = $nodes.Count
                                        unavailable = $_.Exception.Message })
            continue
        }
        $isFocus = ($hwnd -eq $focusHwnd)
        $procName = try { (Get-Process -Id $procId).ProcessName } catch { '' }
        if ($opts.app -and ($procName -notlike "*$($opts.app)*")) { continue }
        if (-not ($opts.all -or $opts.app) -and -not $isFocus) { continue }
        # A closing window, or one whose app is busy (Notepad while its teaching tip is open),
        # makes UIA throw. Say so instead of silently leaving the window out.
        try { $cached = $top.GetUpdatedCache($cr) } catch {
            [void]$tops.Add([ordered]@{ app = $procName; pid = $hwnd; title = $title; focus = $isFocus
                                        start = $nodes.Count; unavailable = $_.Exception.Message })
            continue
        }
        [void]$tops.Add([ordered]@{ app = $procName; pid = $hwnd; title = $title; focus = $isFocus
                                    start = $nodes.Count })
        if ($cached.CachedChildren.Count -eq 0 -and $null -ne $RawWalker.GetFirstChild($top)) {
            Walk-Live $top @(0) 0 '' $hwnd $nodes $limit (New-ElementCache)
        } else {
            Walk $cached @(0) 0 '' $hwnd $nodes $limit
        }
    }
    # An open Win32 menu has the keyboard and the user's attention: list it as the focused window.
    foreach ($menu in [Desk.Msaa]::Menus()) {
        $items = @(Get-MsaaNodes $menu @(0) 0 ([long]$menu) 'menu item' | Where-Object { $_.name })
        if (-not $items) { continue }
        foreach ($t in $tops) { $t.focus = $false }
        [void]$tops.Add([ordered]@{ app = 'menu'; pid = [long]$menu; title = 'menu'; focus = $true; start = $nodes.Count })
        foreach ($n in $items) { [void]$nodes.Add($n) }
    }
    @{ windows = @(); tops = $tops; nodes = $nodes; truncated = $script:truncated }
}

function Walk($el, $path, $depth, $parentName, $hwnd, $nodes, $limit) {
    if ($script:truncated -eq 'time') { return }
    if ($script:walkClock.Elapsed.TotalSeconds -gt $WalkSeconds) { $script:truncated = 'time'; return }
    if ($script:visited -ge $MaxVisit -or ($nodes.Count - $script:offscreen) -ge $limit) { $script:truncated = $true; return }
    $script:visited++
    $node = Describe $el $path $depth $hwnd
    $emit = Wanted $node $parentName ($el.CachedChildren.Count -eq 0)
    if ($emit -and $node.offscreen) { $emit = $script:offscreen -lt 300; if ($emit) { $script:offscreen++ } }
    if ($emit) { [void]$nodes.Add($node) }
    $childDepth = if ($emit) { $depth + 1 } else { $depth }
    $pname = if ($emit) { $node.name } else { $parentName }
    $i = 0
    foreach ($ch in $el.CachedChildren) {
        Walk $ch ($path + $i) $childDepth $pname $hwnd $nodes $limit
        $i++
    }
    if ($el.CachedChildren.Count -eq 0 -and (Cached $el $AE::ClassNameProperty) -eq 'SysListView32') {
        foreach ($n in (Get-MsaaNodes (Cached $el $AE::NativeWindowHandleProperty) $path $childDepth $hwnd)) {
            if ($n.name -and ($nodes.Count - $script:offscreen) -lt $limit) { [void]$nodes.Add($n) }
        }
    }
}

# Items of a SysListView32 read through MSAA (see Get-MsaaNodes): find them the same way.
function Resolve-Msaa($spec) {
    if ($spec.role -eq 'menu item' -and ([Desk.Msaa]::Menus() | Where-Object { [long]$_ -eq [long]$spec.pid })) {
        foreach ($n in (Get-MsaaNodes $spec.pid @() 0 $spec.pid 'menu item')) {
            if ($n.name -eq $spec.name -and $n.bbox) { return @{ bbox = $n.bbox; showing = $true } }
        }
    }
    if ($spec.role -ne 'list item') { return $null }
    $top = $AE::FromHandle([IntPtr][long]$spec.pid)
    $cond = New-Object Windows.Automation.PropertyCondition($AE::ClassNameProperty, 'SysListView32')
    foreach ($lv in $top.FindAll([Windows.Automation.TreeScope]::Subtree, $cond)) {
        foreach ($n in (Get-MsaaNodes $lv.Current.NativeWindowHandle @() 0 $spec.pid)) {
            if ($n.name -eq $spec.name -and $n.bbox) { return @{ bbox = $n.bbox; showing = -not $n.offscreen } }
        }
    }
    $null
}

function Find-Node($spec) {
    $top = $AE::FromHandle([IntPtr][long]$spec.pid)
    $cr = New-Cache
    $cr.AutomationElementMode = [Windows.Automation.AutomationElementMode]::Full
    $cached = $top.GetUpdatedCache($cr)
    if ($cached.CachedChildren.Count -eq 0) {
        # Live walk for MSAA-proxied windows (see Walk-Live).
        $crEl = New-ElementCache
        $crEl.AutomationElementMode = [Windows.Automation.AutomationElementMode]::Full
        $el = $top
        foreach ($i in ($spec.path | Select-Object -Skip 1)) {
            $ch = $RawWalker.GetFirstChild($el)
            for ($k = 0; $k -lt $i -and $null -ne $ch; $k++) { $ch = $RawWalker.GetNextSibling($ch) }
            $el = $ch
            if ($null -eq $el) { break }
        }
        if ($el) {
            $c = $el.GetUpdatedCache($crEl)
            if ((Get-Role $c) -eq $spec.role -and ([string]$c.Cached.Name).Trim() -eq $spec.name) { return $c }
        }
        throw "$($spec.role) '$($spec.name)' no longer exists"
    }
    $el = $cached
    foreach ($i in ($spec.path | Select-Object -Skip 1)) {
        $kids = $el.CachedChildren
        if ($null -eq $kids -or $i -ge $kids.Count) { $el = $null; break }
        $el = $kids[$i]
    }
    if ($el -and (Get-Role $el) -eq $spec.role -and ([string]$el.Cached.Name).Trim() -eq $spec.name) { return $el }
    $stack = New-Object System.Collections.Stack
    $stack.Push($cached)
    while ($stack.Count) {
        $e = $stack.Pop()
        if ((Get-Role $e) -eq $spec.role -and ([string]$e.Cached.Name).Trim() -eq $spec.name) { return $e }
        foreach ($ch in $e.CachedChildren) { $stack.Push($ch) }
    }
    throw "$($spec.role) '$($spec.name)' no longer exists"
}

function Invoke-Uia($op, $arg, $text) {
    $spec = if ($arg -is [string]) { $arg | ConvertFrom-Json } else { $arg }
    switch ($op) {
        'snapshot' { return Snapshot $spec }
        # Diagnostics for windows whose contents do not show up.
        'probe' {
            $out = [ordered]@{ apartment = [Threading.Thread]::CurrentThread.GetApartmentState().ToString() }
            foreach ($top in (Get-TopWindows)) {
                $name = try { $top.Current.Name } catch { "?" }
                if ($spec.title -and $name -notlike "*$($spec.title)*") { continue }
                $row = [ordered]@{ hwnd = $top.Current.NativeWindowHandle; framework = $top.Current.FrameworkId
                                   class = $top.Current.ClassName }
                try { $row.cached = $top.GetUpdatedCache((New-Cache)).CachedChildren.Count } catch { $row.cached = "error: $($_.Exception.Message)" }
                try { $row.control = $top.FindAll([Windows.Automation.TreeScope]::Children, $ControlView).Count } catch { $row.control = "error: $($_.Exception.Message)" }
                try { $row.raw = $top.FindAll([Windows.Automation.TreeScope]::Children, [Windows.Automation.Condition]::TrueCondition).Count } catch { $row.raw = "error: $($_.Exception.Message)" }
                try { $f = $RawWalker.GetFirstChild($top); $row.rawfirst = if ($f) { "$($f.Current.ControlType.ProgrammaticName) '$($f.Current.Name)'" } else { $null } } catch { $row.rawfirst = "error: $($_.Exception.Message)" }
                $out[$name] = $row
            }
            return $out
        }
        'resolve' {
            $msaa = Resolve-Msaa $spec
            if ($msaa) { return $msaa }
            $el = Find-Node $spec
            $r = $el.Current.BoundingRectangle
            if ($r.IsEmpty) { throw 'element has no on-screen extents' }
            return @{ bbox = @([int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height); showing = -not $el.Current.IsOffscreen }
        }
        'scroll' {
            $el = Find-Node $spec
            $p = $null
            if ($el.TryGetCurrentPattern([Windows.Automation.ScrollItemPattern]::Pattern, [ref]$p)) { $p.ScrollIntoView() }
            return @{ done = [bool]$p }
        }
        'set-text' {
            $el = Find-Node $spec
            $p = $null
            if (-not $el.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern, [ref]$p)) { throw 'element is not editable' }
            $p.SetValue($text)
            return @{ done = $true }
        }
        'action' {
            $el = Find-Node $spec
            $available = @()
            $map = [ordered]@{ invoke = [Windows.Automation.InvokePattern]; toggle = [Windows.Automation.TogglePattern]
                               select = [Windows.Automation.SelectionItemPattern]; expand = [Windows.Automation.ExpandCollapsePattern] }
            foreach ($k in $map.Keys) {
                $p = $null
                if ($el.TryGetCurrentPattern($map[$k]::Pattern, [ref]$p)) { $available += $k }
            }
            if (-not $available) { throw 'element has no actions' }
            $want = if ($text) { $text } else { $available[0] }
            if ($available -notcontains $want) { throw "no action '$want'; available: $($available -join ', ')" }
            $p = $el.GetCurrentPattern($map[$want]::Pattern)
            switch ($want) { 'invoke' { $p.Invoke() } 'toggle' { $p.Toggle() } 'select' { $p.Select() } 'expand' { $p.Expand() } }
            return @{ done = $want; actions = $available }
        }
    }
    throw "unknown uia op $op"
}
