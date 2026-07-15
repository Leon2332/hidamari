<p align="center"><img src="https://raw.githubusercontent.com/jeffshee/hidamari/master/res/hidamari.svg" width="256"></p>

<p align="center">Video wallpaper for Linux. Written in Python. 🐍</p>  
<p align="center">Hidamari 日溜まり【ひだまり】(n) sunny spot; exposure to the sun</p>

# Hidamari　ーひだまりー
- Original: [jeffshee/hidamari](https://github.com/jeffshee/hidamari)
- My objective: Port to Gtk4/adwaita.

# Modifications

## Modified files:

<table>
  <tr><th>FILE</th>                                         <th>DESCRIPTION</th></tr>
  <tr><td>src/hidamari/assets/ control.ui</td>	            <td>GTK4/libadwaita UI (FlowBox, etc.)</td></tr>
  <tr><td>src/hidamari/gui/control.py</td>	                <td>Control panel port + async Apply</td></tr>
  <tr><td>src/hidamari/gui/gui_utils.py</td>	              <td>Larger thumbnails / async load</td></tr>
  <tr><td>src/hidamari/menu.py</td>	                        <td>GTK4 context menu + GTK3 tray isolation</td></tr>
  <tr><td>src/hidamari/monitor.py</td>	                    <td>GDK 4 monitors</td></tr>
  <tr><td>src/hidamari/commons.py</td>	                    <td>Lazy monitor config (no Gdk at import)</td></tr>
  <tr><td>src/hidamari/utils.py</td>	                      <td>Config refresh; EWMH window handler (no libwnck)</td></tr>
  <tr><td>src/hidamari/server.py</td>	                      <td>Lazy process imports; safer player quit</td></tr>
  <tr><td>src/hidamari/player/base_player.py</td>	          <td>GTK4 monitors; pure X11 surfaces</td></tr>
  <tr><td>src/hidamari/player/video_player.py</td>	        <td>Pure X11 + VLC wallpaper path</td></tr>
  <tr><td>src/hidamari/player/web_player.py</td>	          <td>GTK4 + WebKit 6</td></tr>
  <tr><td>docs/dev.md</td>	                                <td>GTK4/libadwaita/VLC deps</td></tr>
</table>

## New files:
<table>
  <tr><th>FILE</th>                                         <th>DESCRIPTION</th></tr>
  <tr><td>src/hidamari/player/x11_window.py</td>	          <td>X11 helpers (desktop hints, primary monitor)</td></tr>
  <tr><td>src/hidamari/player/x11_surface.py</td>	          <td>Managed DESKTOP+BELOW depth-24 wallpaper window</td></tr>
</table>
