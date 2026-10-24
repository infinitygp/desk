import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const IFACE = `
<node>
  <interface name="org.desk.Helper">
    <method name="GetWindows">
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="Activate">
      <arg type="s" direction="in" name="query"/>
      <arg type="s" direction="out" name="title"/>
    </method>
    <method name="SetClipboard">
      <arg type="s" direction="in" name="text"/>
    </method>
    <method name="GetShell">
      <arg type="s" direction="out" name="json"/>
    </method>
  </interface>
</node>`;

export default class DeskHelper extends Extension {
    enable() {
        this._impl = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._impl.export(Gio.DBus.session, '/org/desk/Helper');
        this._nameId = Gio.bus_own_name_on_connection(
            Gio.DBus.session, 'org.desk.Helper', Gio.BusNameOwnerFlags.NONE, null, null);
        // GNOME opens the overview after login, and windows started from outside the shell
        // (desk exec --gui) do not close it. Start on the plain desktop instead.
        if (Main.layoutManager._startingUp) {
            this._startupId = Main.layoutManager.connect('startup-complete', () => {
                Main.overview.hide();
                Main.layoutManager.disconnect(this._startupId);
                this._startupId = 0;
            });
        } else {
            // GNOME 51 loads extensions asynchronously, now and then only after startup-complete,
            // with the login's overview already open (a fedora-45-gnome warm state kept it).
            Main.overview.hide();
        }
    }

    disable() {
        if (this._startupId) {
            Main.layoutManager.disconnect(this._startupId);
            this._startupId = 0;
        }
        Gio.bus_unown_name(this._nameId);
        this._impl.unexport();
        this._impl = null;
    }

    // Windows in stacking order, bottom first.
    GetWindows() {
        const focus = global.display.focus_window;
        const windows = global.get_window_actors().map(actor => {
            const w = actor.meta_window;
            const f = w.get_frame_rect();
            const b = w.get_buffer_rect();
            return {
                title: w.get_title(),
                wm_class: w.get_wm_class(),
                pid: w.get_pid(),
                x11: w.get_client_type() === Meta.WindowClientType.X11,
                minimized: w.minimized,
                focus: w === focus,
                frame: [f.x, f.y, f.width, f.height],
                buffer: [b.x, b.y, b.width, b.height],
            };
        });
        return JSON.stringify(windows);
    }

    _find(query) {
        const q = query.toLowerCase();
        return global.get_window_actors().map(a => a.meta_window).reverse().find(w =>
            (w.get_wm_class() ?? '').toLowerCase().includes(q) || (w.get_title() ?? '').toLowerCase().includes(q));
    }

    // Bring the topmost window whose application (WM class) or title contains `query` to the
    // front, as clicking it in the overview would; returns its title, or "" if there is none.
    Activate(query) {
        const hit = this._find(query);
        if (!hit)
            return '';
        Main.overview.hide();
        if (hit.minimized)
            hit.unminimize();
        hit.activate(global.get_current_time());
        return hit.get_title() ?? '';
    }

    // What of the shell itself lies over the windows: the overview, a notification banner.
    // A failed test step names them, so a prompt of the system is not taken for a bug.
    GetShell() {
        let banner = null;
        try {
            // Private: the notification whose banner is shown now (GNOME 50's messageTray.js).
            const n = Main.messageTray._notification;
            if (n)
                banner = {title: n.title ?? '', body: n.body ?? '', source: n.source?.title ?? ''};
        } catch (e) {
            logError(e, 'desk-helper: notification banner');
        }
        return JSON.stringify({overview: Main.overview.visible, banner});
    }

    // The clipboard, which only the compositor can set without a focused window of its own
    // (desk set-text pastes what the keyboard cannot type).
    SetClipboard(text) {
        St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text);
    }
}
