# UI Automation side of `desk a11y` on Windows: the same JSON protocol as desk/guest_a11y.py
# (snapshot, resolve, action, set-text, scroll). UIA reports screen pixels directly; the test
# images run at 100 % scaling so they match the screenshots.
$AE = [Windows.Automation.AutomationElement]
$ControlView = [Windows.Automation.Automation]::ControlViewCondition
$Walker = [Windows.Automation.TreeWalker]::ControlViewWalker

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

function Get-Role($el) {
    $name = $el.Cached.ControlType.ProgrammaticName -replace '^ControlType\.', ''
    if ($Roles.ContainsKey($name)) { $Roles[$name] } else { $name.ToLower() }
}

function New-Cache {
    $cr = New-Object Windows.Automation.CacheRequest
    $cr.TreeScope = [Windows.Automation.TreeScope]::Subtree
    $cr.TreeFilter = $ControlView
    foreach ($p in 'NameProperty', 'ControlTypeProperty', 'BoundingRectangleProperty', 'IsOffscreenProperty',
                   'IsEnabledProperty', 'HasKeyboardFocusProperty', 'ProcessIdProperty', 'NativeWindowHandleProperty',
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

function Wanted($node, $parentName) {
    if ($null -eq $node.bbox) { return $false }
    if ($Interactive -contains $node.role) { return $true }
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
        Walk $cached @(0) 0 '' $hwnd $nodes $limit
    }
    @{ windows = @(); tops = $tops; nodes = $nodes; truncated = $script:truncated }
}

function Walk($el, $path, $depth, $parentName, $hwnd, $nodes, $limit) {
    if ($script:visited -ge $MaxVisit -or ($nodes.Count - $script:offscreen) -ge $limit) { $script:truncated = $true; return }
    $script:visited++
    $node = Describe $el $path $depth $hwnd
    $emit = Wanted $node $parentName
    if ($emit -and $node.offscreen) { $emit = $script:offscreen -lt 300; if ($emit) { $script:offscreen++ } }
    if ($emit) { [void]$nodes.Add($node) }
    $childDepth = if ($emit) { $depth + 1 } else { $depth }
    $pname = if ($emit) { $node.name } else { $parentName }
    $i = 0
    foreach ($ch in $el.CachedChildren) {
        Walk $ch ($path + $i) $childDepth $pname $hwnd $nodes $limit
        $i++
    }
}

function Find-Node($spec) {
    $top = $AE::FromHandle([IntPtr][long]$spec.pid)
    $cr = New-Cache
    $cr.AutomationElementMode = [Windows.Automation.AutomationElementMode]::Full
    $cached = $top.GetUpdatedCache($cr)
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
        'resolve' {
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
