# -*- encoding: utf-8 -*-
#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2017 Marek Marczykowski-Górecki
#                               <marmarek@invisiblethingslab.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation; either version 2.1 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License along
# with this program; if not, see <http://www.gnu.org/licenses/>.

""" GUI/AUDIO daemon launcher tool"""

import os
import signal
import subprocess
import asyncio
import re
import functools
import sys
import xcffib
import xcffib.xproto  # pylint: disable=unused-import

import daemon.pidfile
import qubesadmin
import qubesadmin.exc
import qubesadmin.tools
import qubesadmin.vm

have_events = False
try:
    # pylint: disable=wrong-import-position
    import qubesadmin.events

    have_events = True
except ImportError:
    pass

GUI_DAEMON_PATH = '/usr/bin/qubes-guid'
PACAT_DAEMON_PATH = '/usr/bin/pacat-simple-vchan'
QUBES_ICON_DIR = '/usr/share/icons/hicolor/128x128/devices'

GUI_DAEMON_OPTIONS = [
    ('allow_fullscreen', 'bool'),
    ('override_redirect_protection', 'bool'),
    ('allow_utf8_titles', 'bool'),
    ('secure_copy_sequence', 'str'),
    ('secure_paste_sequence', 'str'),
    ('windows_count_limit', 'int'),
    ('trayicon_mode', 'str'),
    ('startup_timeout', 'int'),
]


def retrieve_gui_daemon_options(vm, guivm):
    '''
    Construct a list of GUI daemon options based on VM features.

    This checks 'gui-*' features on the VM, and if they're absent,
    'gui-default-*' features on the GuiVM.
    '''

    options = {}

    for name, kind in GUI_DAEMON_OPTIONS:
        feature_value = vm.features.get(
            'gui-' + name.replace('_', '-'), None)
        if feature_value is None:
            feature_value = guivm.features.get(
                'gui-default-' + name.replace('_', '-'), None)
        if feature_value is None:
            continue

        if kind == 'bool':
            value = bool(feature_value)
        elif kind == 'int':
            value = int(feature_value)
        elif kind == 'str':
            value = feature_value
        else:
            assert False, kind

        options[name] = value
    return options


def serialize_gui_daemon_options(options):
    '''
    Prepare configuration file content for GUI daemon. Currently uses libconfig
    format.
    '''

    lines = [
        '# Auto-generated file, do not edit!',
        '',
        'global: {',
    ]
    for name, kind in GUI_DAEMON_OPTIONS:
        if name in options:
            value = options[name]
            if kind == 'bool':
                serialized = 'true' if value else 'false'
            elif kind == 'int':
                serialized = str(value)
            elif kind == 'str':
                serialized = escape_config_string(value)
            else:
                assert False, kind

            lines.append('  {} = {};'.format(name, serialized))
    lines.append('}')
    lines.append('')
    return '\n'.join(lines)


NON_ASCII_RE = re.compile(r'[^\x00-\x7F]')
UNPRINTABLE_CHARACTER_RE = re.compile(r'[\x00-\x1F\x7F]')

def escape_config_string(value):
    '''
    Convert a string to libconfig format.

    Format specification:
    http://www.hyperrealm.com/libconfig/libconfig_manual.html#String-Values

    See dump_string() for python-libconf:
    https://github.com/Grk0/python-libconf/blob/master/libconf.py
    '''

    assert not NON_ASCII_RE.match(value), 'expected an ASCII string: {!r}'.format(value)

    value = (
        value.replace('\\', '\\\\')
             .replace('"', '\\"')
             .replace('\f', r'\f')
             .replace('\n', r'\n')
             .replace('\r', r'\r')
             .replace('\t', r'\t')
    )
    value = UNPRINTABLE_CHARACTER_RE.sub(
          lambda m: r'\x{:02x}'.format(ord(m.group(0))),
        value)
    return '"' + value + '"'


# "LVDS connected 1024x768+0+0 (normal left inverted right) 304mm x 228mm"
REGEX_OUTPUT = re.compile(r"""
        (?x)                           # ignore whitespace
        ^                              # start of string
        (?P<output>[A-Za-z0-9\-]*)[ ]  # LVDS VGA etc
        (?P<connect>(dis)?connected)   # dis/connected
        ([ ]
        (?P<primary>(primary)?)[ ]?
        ((                             # a group
           (?P<width>\d+)x             # either 1024x768+0+0
           (?P<height>\d+)[+]
           (?P<x>\d+)[+]
           (?P<y>\d+)
         )|[\D])                       # or not a digit
        ([ ]\(.*\))?[ ]?               # ignore options
        (                              #  304mm x 228mm
           (?P<width_mm>\d+)mm[ ]x[ ]
           (?P<height_mm>\d+)mm
        )?
        .*                             # ignore rest of line
        )?                             # everything after (dis)connect is optional
        """)


def get_monitor_layout():
    """Get list of monitors and their size/position"""
    outputs = []

    for line in subprocess.Popen(
            ['xrandr', '-q'], stdout=subprocess.PIPE).stdout:
        line = line.decode()
        if not line.startswith("Screen") and not line.startswith(" "):
            output_params = REGEX_OUTPUT.match(line).groupdict()
            if output_params['width']:
                phys_size = ""
                if output_params['width_mm'] and int(output_params['width_mm']):
                    # don't provide real values for privacy reasons - see
                    # #1951 for details
                    dpi = (int(output_params['width']) * 254 //
                           int(output_params['width_mm']) // 10)
                    if dpi > 300:
                        dpi = 300
                    elif dpi > 200:
                        dpi = 200
                    elif dpi > 150:
                        dpi = 150
                    else:
                        # if lower, don't provide this info to the VM at all
                        dpi = 0
                    if dpi:
                        # now calculate dimensions based on approximate DPI
                        phys_size = " {} {}".format(
                            int(output_params['width']) * 254 // dpi // 10,
                            int(output_params['height']) * 254 // dpi // 10,
                        )
                outputs.append("%s %s %s %s%s\n" % (
                    output_params['width'],
                    output_params['height'],
                    output_params['x'],
                    output_params['y'],
                    phys_size,
                ))
    return outputs


def set_keyboard_layout(vm):
    """Set layout configuration into features for Gui admin extension"""
    try:
        # Examples of 'xprop -root _XKB_RULES_NAMES' output values:
        # "evdev", "pc105", "fr", "oss", ""
        # "evdev", "pc105", "pl,us", ",", "grp:win_switch,compose:caps"

        # We use the first layout provided
        xkb_re = r'_XKB_RULES_NAMES\(STRING\) = ' \
                 r'\"(.*)\", \"(.*)\", \"(.*)\", \"(.*)\", \"(.*)\"\n'
        xkb_rules_names = subprocess.check_output(
            ['xprop', '-root', '_XKB_RULES_NAMES']).decode()
        xkb_parsed = re.match(xkb_re, xkb_rules_names)
        if xkb_parsed:
            xkb_layout = [x.split(',')[0] for x in xkb_parsed.groups()[2:4]]
            # We keep all options
            xkb_layout.append(xkb_parsed.group(5))
            keyboard_layout = '+'.join(xkb_layout)
            vm.features['keyboard-layout'] = keyboard_layout
        else:
            vm.log.warning('Failed to parse layout for %s', vm)
    except subprocess.CalledProcessError as e:
        vm.log.warning('Failed to set layout for %s: %s', vm, str(e))


class DAEMONLauncher:
    """Launch GUI/AUDIO daemon for VMs"""

    def __init__(self, app: qubesadmin.app.QubesBase):
        """ Initialize DAEMONLauncher.

        :param app: :py:class:`qubesadmin.Qubes` instance
        """
        self.app = app
        self.started_processes = {}

    @asyncio.coroutine
    def send_monitor_layout(self, vm, layout=None, startup=False):
        """Send monitor layout to a given VM

        This function is a coroutine.

        :param vm: VM to which send monitor layout
        :param layout: monitor layout to send; if None, fetch it from
            local X server.
        :param startup:
        :return: None
        """
        # pylint: disable=no-self-use
        if vm.features.check_with_template('no-monitor-layout', False) \
                or not vm.is_running():
            return

        if layout is None:
            layout = get_monitor_layout()
            if not layout:
                return

        vm.log.info('Sending monitor layout')

        if not startup:
            with open(self.guid_pidfile(vm.xid)) as pidfile:
                pid = int(pidfile.read())
            os.kill(pid, signal.SIGHUP)
            try:
                with open(self.guid_pidfile(vm.stubdom_xid)) as pidfile:
                    pid = int(pidfile.read())
                os.kill(pid, signal.SIGHUP)
            except FileNotFoundError:
                pass

        try:
            yield from asyncio.get_event_loop(). \
                run_in_executor(None,
                                functools.partial(
                                    vm.run_service_for_stdio,
                                    'qubes.SetMonitorLayout',
                                    input=''.join(layout).encode(),
                                    autostart=False))
        except subprocess.CalledProcessError as e:
            vm.log.warning('Failed to send monitor layout: %s', e.stderr)

    def send_monitor_layout_all(self):
        """Send monitor layout to all (running) VMs"""
        monitor_layout = get_monitor_layout()
        for vm in self.app.domains:
            if getattr(vm, 'guivm', None) != vm.app.local_name:
                continue
            if vm.klass == 'AdminVM':
                continue
            if vm.is_running():
                if not vm.features.check_with_template('gui', True):
                    continue
                asyncio.ensure_future(self.send_monitor_layout(vm,
                                                               monitor_layout))

    @staticmethod
    def kde_guid_args(vm):
        """Return KDE-specific arguments for gui-daemon, if applicable"""

        guid_cmd = []
        # Avoid using environment variables for checking the current session,
        #  because this script may be called with cleared env (like with sudo).
        if subprocess.check_output(
                ['xprop', '-root', '-notype', 'KWIN_RUNNING']) == \
                b'KWIN_RUNNING = 0x1\n':
            # native decoration plugins is used, so adjust window properties
            # accordingly
            guid_cmd += ['-T']  # prefix window titles with VM name
            # get owner of X11 session
            session_owner = None
            for line in subprocess.check_output(['xhost']).splitlines():
                if line == b'SI:localuser:root':
                    pass
                elif line.startswith(b'SI:localuser:'):
                    session_owner = line.split(b':')[2].decode()
            if session_owner is not None:
                data_dir = os.path.expanduser(
                    '~{}/.local/share'.format(session_owner))
            else:
                # fallback to current user
                data_dir = os.path.expanduser('~/.local/share')

            guid_cmd += ['-p',
                         '_KDE_NET_WM_COLOR_SCHEME=s:{}'.format(
                             os.path.join(data_dir,
                                          'qubes-kde',
                                          vm.label.name + '.colors'))]
        return guid_cmd

    def common_guid_args(self, vm):
        """Common qubes-guid arguments for PV(H), HVM and Stubdomain"""

        guid_cmd = [GUI_DAEMON_PATH,
                    '-N', vm.name,
                    '-c', vm.label.color,
                    '-i', os.path.join(QUBES_ICON_DIR, vm.label.icon) + '.png',
                    '-l', str(vm.label.index)]

        if vm.debug:
            guid_cmd += ['-v', '-v']
            #       elif not verbose:
        else:
            guid_cmd += ['-q']

        if vm.features.check_with_template('rpc-clipboard', False):
            guid_cmd.extend(['-Q'])

        guivm = self.app.domains[vm.guivm]
        options = retrieve_gui_daemon_options(vm, guivm)
        config = serialize_gui_daemon_options(options)
        config_path = self.guid_config_file(vm.xid)
        self.write_guid_config(config_path, config)
        guid_cmd.extend(['-C', config_path])

        guid_cmd += self.kde_guid_args(vm)
        return guid_cmd

    @staticmethod
    def write_guid_config(config_path, config):
        """Write guid configuration to a file"""
        with open(config_path, 'w') as config_file:
            config_file.write(config)

    @staticmethod
    def guid_pidfile(xid):
        """Helper function to construct a GUI pidfile path"""
        return '/var/run/qubes/guid-running.{}'.format(xid)

    @staticmethod
    def guid_config_file(xid):
        """Helper function to construct a GUI configuration file path"""
        return '/var/run/qubes/guid-conf.{}'.format(xid)

    @staticmethod
    def pacat_pidfile(xid):
        """Helper function to construct an AUDIO pidfile path"""
        return '/var/run/qubes/pacat.{}'.format(xid)

    @staticmethod
    def pacat_domid(vm):
        """Determine target domid for an AUDIO daemon"""
        xid = vm.stubdom_xid \
                if vm.features.check_with_template('audio-model', False) \
                and vm.virt_mode == 'hvm' \
                else vm.xid
        return xid

    @asyncio.coroutine
    def start_gui_for_vm(self, vm, monitor_layout=None):
        """Start GUI daemon (qubes-guid) connected directly to a VM

        This function is a coroutine.

        :param vm: VM for which start GUI daemon
        :param monitor_layout: monitor layout to send; if None, fetch it from
            local X server.
        """
        guid_cmd = self.common_guid_args(vm)
        guid_cmd.extend(['-d', str(vm.xid)])

        if vm.virt_mode == 'hvm':
            guid_cmd.extend(['-n'])

            stubdom_guid_pidfile = self.guid_pidfile(vm.stubdom_xid)
            if not vm.debug and os.path.exists(stubdom_guid_pidfile):
                # Terminate stubdom guid once "real" gui agent connects
                with open(stubdom_guid_pidfile, 'r') as pidfile:
                    stubdom_guid_pid = pidfile.read().strip()
                guid_cmd += ['-K', stubdom_guid_pid]

        vm.log.info('Starting GUI')

        yield from asyncio.create_subprocess_exec(*guid_cmd)

        yield from self.send_monitor_layout(vm, layout=monitor_layout,
                                            startup=True)

    @asyncio.coroutine
    def start_gui_for_stubdomain(self, vm, force=False):
        """Start GUI daemon (qubes-guid) connected to a stubdomain

        This function is a coroutine.
        """
        want_stubdom = force
        if not want_stubdom and \
                vm.features.check_with_template('gui-emulated', False):
            want_stubdom = True
        # if no 'gui' or 'gui-emulated' feature set at all, use emulated GUI
        if not want_stubdom and \
                vm.features.check_with_template('gui', None) is None and \
                vm.features.check_with_template('gui-emulated', None) is None:
            want_stubdom = True
        if not want_stubdom and vm.debug:
            want_stubdom = True
        if not want_stubdom:
            return
        if os.path.exists(self.guid_pidfile(vm.stubdom_xid)):
            return
        vm.log.info('Starting GUI (stubdomain)')
        guid_cmd = self.common_guid_args(vm)
        guid_cmd.extend(['-d', str(vm.stubdom_xid), '-t', str(vm.xid)])

        yield from asyncio.create_subprocess_exec(*guid_cmd)

    @asyncio.coroutine
    def start_audio_for_vm(self, vm):
        """Start AUDIO daemon (pacat-simple-vchan) connected directly to a VM

        This function is a coroutine.

        :param vm: VM for which start AUDIO daemon
        """
        # pylint: disable=no-self-use
        pacat_cmd = [PACAT_DAEMON_PATH, '-l', self.pacat_domid(vm), vm.name]
        vm.log.info('Starting AUDIO')

        yield from asyncio.create_subprocess_exec(*pacat_cmd)

    @asyncio.coroutine
    def start_gui(self, vm, force_stubdom=False, monitor_layout=None):
        """Start GUI daemon regardless of start event.

        This function is a coroutine.

        :param vm: VM for which GUI daemon should be started
        :param force_stubdom: Force GUI daemon for stubdomain, even if the
            one for target AppVM is running.
        :param monitor_layout: monitor layout configuration
        """
        guivm = getattr(vm, 'guivm', None)
        if guivm != vm.app.local_name:
            vm.log.info('GUI connected to {}. Skipping.'.format(guivm))
            return

        if vm.virt_mode == 'hvm':
            yield from self.start_gui_for_stubdomain(vm, force=force_stubdom)

        if not vm.features.check_with_template('gui', True):
            return

        if not os.path.exists(self.guid_pidfile(vm.xid)):
            yield from self.start_gui_for_vm(vm, monitor_layout=monitor_layout)

    @asyncio.coroutine
    def start_audio(self, vm):
        """Start AUDIO daemon regardless of start event.

        This function is a coroutine.

        :param vm: VM for which AUDIO daemon should be started
        """
        audiovm = getattr(vm, 'audiovm', None)
        if audiovm != vm.app.local_name:
            vm.log.info('AUDIO connected to {}. Skipping.'.format(audiovm))
            return

        if not vm.features.check_with_template('audio', True):
            return

        xid = self.pacat_domid(vm)
        if not os.path.exists(self.pacat_pidfile(xid)):
            yield from self.start_audio_for_vm(vm)

    def on_domain_spawn(self, vm, _event, **kwargs):
        """Handler of 'domain-spawn' event, starts GUI daemon for stubdomain"""
        try:
            if getattr(vm, 'guivm', None) != vm.app.local_name:
                return
            if not vm.features.check_with_template('gui', True):
                return
            if vm.virt_mode == 'hvm' and \
                    kwargs.get('start_guid', 'True') == 'True':
                asyncio.ensure_future(self.start_gui_for_stubdomain(vm))
        except qubesadmin.exc.QubesException as e:
            vm.log.warning('Failed to start GUI for %s: %s', vm.name, str(e))

    def on_domain_start(self, vm, _event, **kwargs):
        """Handler of 'domain-start' event, starts GUI/AUDIO daemon for
        actual VM """
        try:
            if getattr(vm, 'guivm', None) == vm.app.local_name and \
                    vm.features.check_with_template('gui', True) and \
                    kwargs.get('start_guid', 'True') == 'True':
                asyncio.ensure_future(self.start_gui_for_vm(vm))
        except qubesadmin.exc.QubesException as e:
            vm.log.warning('Failed to start GUI for %s: %s', vm.name, str(e))

        try:
            if getattr(vm, 'audiovm', None) == vm.app.local_name and \
                    vm.features.check_with_template('audio', True) and \
                    kwargs.get('start_audio', 'True') == 'True':
                asyncio.ensure_future(self.start_audio_for_vm(vm))
        except qubesadmin.exc.QubesException as e:
            vm.log.warning('Failed to start AUDIO for %s: %s', vm.name, str(e))

    def on_connection_established(self, _subject, _event, **_kwargs):
        """Handler of 'connection-established' event, used to launch GUI/AUDIO
        daemon for domains started before this tool. """

        monitor_layout = get_monitor_layout()
        self.app.domains.clear_cache()
        for vm in self.app.domains:
            if vm.klass == 'AdminVM':
                continue

            power_state = vm.get_power_state()
            if power_state == 'Running':
                asyncio.ensure_future(
                    self.start_gui(vm, monitor_layout=monitor_layout))
                asyncio.ensure_future(self.start_audio(vm))
            elif power_state == 'Transient':
                # it is still starting, we'll get 'domain-start'
                # event when fully started
                if vm.virt_mode == 'hvm':
                    asyncio.ensure_future(
                        self.start_gui_for_stubdomain(vm))

    def on_domain_stopped(self, vm, _event, **_kwargs):
        """Handler of 'domain-stopped' event, cleans up"""
        self.cleanup_guid(vm.xid)
        if vm.virt_mode == 'hvm':
            self.cleanup_guid(vm.stubdom_xid)

    def cleanup_guid(self, xid):
        """
        Clean up after qubes-guid. Removes the auto-generated configuration
        file, if any.
        """

        config_path = self.guid_config_file(xid)
        if os.path.exists(config_path):
            os.unlink(config_path)

    def register_events(self, events):
        """Register domain startup events in app.events dispatcher"""
        events.add_handler('domain-spawn', self.on_domain_spawn)
        events.add_handler('domain-start', self.on_domain_start)
        events.add_handler('connection-established',
                           self.on_connection_established)
        events.add_handler('domain-stopped', self.on_domain_stopped)


def x_reader(conn, callback):
    """Try reading something from X connection to check if it's still alive.
    In case it isn't, call *callback*.
    """
    try:
        conn.poll_for_event()
    except xcffib.ConnectionException:
        callback()


if 'XDG_RUNTIME_DIR' in os.environ:
    pidfile_path = os.path.join(os.environ['XDG_RUNTIME_DIR'],
                                'qvm-start-daemon.pid')
else:
    pidfile_path = os.path.join(os.environ.get('HOME', '/'),
                                '.qvm-start-daemon.pid')

parser = qubesadmin.tools.QubesArgumentParser(
    description='start GUI for qube(s)', vmname_nargs='*')
parser.add_argument('--watch', action='store_true',
                    help='Keep watching for further domains'
                         ' startups, must be used with --all')
parser.add_argument('--force-stubdomain', action='store_true',
                    help='Start GUI to stubdomain-emulated VGA,'
                         ' even if gui-agent is running in the VM')
parser.add_argument('--pidfile', action='store', default=pidfile_path,
                    help='Pidfile path to create in --watch mode')
parser.add_argument('--notify-monitor-layout', action='store_true',
                    help='Notify running instance in --watch mode'
                         ' about changed monitor layout')
parser.add_argument('--set-keyboard-layout', action='store_true',
                    help='Set keyboard layout values into GuiVM features.'
                         'This option is implied by --watch')
# Add it for the help only
parser.add_argument('--force', action='store_true', default=False,
                    help='Force running daemon without enabled services'
                         ' \'guivm-gui-agent\' or \'audiovm-audio-agent\'')


def main(args=None):
    """ Main function of qvm-start-daemon tool"""
    only_if_service_enabled = ['guivm-gui-agent', 'audiovm-audio-agent']
    enabled_services = [service for service in only_if_service_enabled if
                        os.path.exists('/var/run/qubes-service/%s' % service)]
    if not enabled_services and '--force' not in sys.argv and \
            not os.path.exists('/etc/qubes-release'):
        print(parser.format_help())
        return
    args = parser.parse_args(args)
    if args.watch and not args.all_domains:
        parser.error('--watch option must be used with --all')
    if args.watch and args.notify_monitor_layout:
        parser.error('--watch cannot be used with --notify-monitor-layout')
    if args.watch and 'guivm-gui-agent' in enabled_services:
        args.set_keyboard_layout = True
    if args.set_keyboard_layout or os.path.exists('/etc/qubes-release'):
        guivm = args.app.domains.get_blind(args.app.local_name)
        set_keyboard_layout(guivm)
    launcher = DAEMONLauncher(args.app)
    if args.watch:
        if not have_events:
            parser.error('--watch option require Python >= 3.5')
        with daemon.pidfile.TimeoutPIDLockFile(args.pidfile):
            loop = asyncio.get_event_loop()
            # pylint: disable=no-member
            events = qubesadmin.events.EventsDispatcher(args.app)
            # pylint: enable=no-member
            launcher.register_events(events)

            events_listener = asyncio.ensure_future(events.listen_for_events())

            for signame in ('SIGINT', 'SIGTERM'):
                loop.add_signal_handler(getattr(signal, signame),
                                        events_listener.cancel)  # pylint: disable=no-member

            loop.add_signal_handler(signal.SIGHUP,
                                    launcher.send_monitor_layout_all)

            conn = xcffib.connect()
            x_fd = conn.get_file_descriptor()
            loop.add_reader(x_fd, x_reader, conn, events_listener.cancel)

            try:
                loop.run_until_complete(events_listener)
            except asyncio.CancelledError:
                pass
            loop.remove_reader(x_fd)
            loop.stop()
            loop.run_forever()
            loop.close()
    elif args.notify_monitor_layout:
        try:
            with open(pidfile_path, 'r') as pidfile:
                pid = int(pidfile.read().strip())
            os.kill(pid, signal.SIGHUP)
        except (FileNotFoundError, ValueError) as e:
            parser.error('Cannot open pidfile {}: {}'.format(pidfile_path,
                                                             str(e)))
    else:
        loop = asyncio.get_event_loop()
        tasks = []
        for vm in args.domains:
            if vm.is_running():
                tasks.append(asyncio.ensure_future(launcher.start_gui(
                    vm, force_stubdom=args.force_stubdomain)))
                tasks.append(asyncio.ensure_future(launcher.start_audio(
                    vm)))
        if tasks:
            loop.run_until_complete(asyncio.wait(tasks))
        loop.stop()
        loop.run_forever()
        loop.close()


if __name__ == '__main__':
    main()
