import os
import time
from optparse import make_option
import atexit
from collections import defaultdict

from django.conf import settings

from django.core.management.base import NoArgsCommand, CommandError

from django_extensions.management.shells import import_objects, ReloaderEventHandler

try:
    from watchdog.observers import Observer
    _listener_enabled = True
except ImportError:
    _listener_enabled = False
else:
    # Threading object which listens for file system changes
    # Keep a reference to this object in the global scope
    # So that we can join it on program exit
    # Failing to do so raises ugly Threading exceptions
    # We may choose to just suppress stderr instead
    # Since waiting for thread to end seems to take time (~1sec)
    observer_thread = Observer()

    def kill_observer_thread():
        # Clean up the filesystem listener thread on program exit
        # This seems to add roughly one second to shut down, so it's not ideal
        if observer_thread.is_alive():
            observer_thread.stop()
            observer_thread.join()

    # Register functions to be called when Python program ends normally
    atexit.register(kill_observer_thread)


class Command(NoArgsCommand):
    option_list = NoArgsCommand.option_list + (
        make_option('--plain', action='store_true', dest='plain',
                    help='Tells Django to use plain Python, not BPython nor IPython.'),
        make_option('--bpython', action='store_true', dest='bpython',
                    help='Tells Django to use BPython, not IPython.'),
        make_option('--ipython', action='store_true', dest='ipython',
                    help='Tells Django to use IPython, not BPython.'),
        make_option('--notebook', action='store_true', dest='notebook',
                    help='Tells Django to use IPython Notebook.'),
        make_option('--no-pythonrc', action='store_true', dest='no_pythonrc',
                    help='Tells Django not to execute PYTHONSTARTUP file'),
        make_option('--print-sql', action='store_true', default=False,
                    help="Print SQL queries as they're executed"),
        make_option('--dont-load', action='append', dest='dont_load', default=[],
                    help='Ignore autoloading of some apps/models. Can be used several times.'),
        make_option('--quiet-load', action='store_true', default=False, dest='quiet_load',
                    help='Do not display loaded models messages'),
        make_option('--autoreload', action='store_true', default=False, dest='autoreload',
                    help='Do not display loaded models messages'),
    )
    help = "Like the 'shell' command but autoloads the models of all installed Django apps."

    requires_model_validation = True

    def handle_noargs(self, **options):
        use_notebook = options.get('notebook', False)
        use_ipython = options.get('ipython', False)
        use_bpython = options.get('bpython', False)
        use_plain = options.get('plain', False)
        use_pythonrc = not options.get('no_pythonrc', True)
        auto_reload = options.get('autoreload') or getattr(settings, 'AUTO_RELOAD_SHELL', False)
        if auto_reload:
            # Transparently reload modules on filesystem changes via threading
            if not _listener_enabled:
                raise CommandError("Watchdog is required to use auto reload shell_plus.  Install via pip. (pip install watchdog)")

            autoreload_path = os.environ.get('VIRTUAL_ENV', getattr(settings, 'PROJECT_ROOT', False))
            if not autoreload_path:
                raise CommandError("""To reload shell_plus automatically,
                                    you must either work in an activated
                                    Python VIRTUALENV or specify a path in your filesystem
                                    as 'PROJECT_ROOT' in your Django settings""")

            def listen_for_changes(shell_globals, project_root, model_globals):
                # Begin thread which listens for file system changes via Watchdog library
                event_handler = ReloaderEventHandler(project_root=project_root, model_globals=model_globals, shell_globals=shell_globals)
                observer_thread.schedule(event_handler, path=project_root, recursive=True)
                observer_thread.start()

        if options.get("print_sql", False):
            # Code from http://gist.github.com/118990
            from django.db.backends import util
            sqlparse = None
            try:
                import sqlparse
            except ImportError:
                pass

            class PrintQueryWrapper(util.CursorDebugWrapper):
                def execute(self, sql, params=()):
                    starttime = time.time()
                    try:
                        return self.cursor.execute(sql, params)
                    finally:
                        execution_time = time.time() - starttime
                        raw_sql = self.db.ops.last_executed_query(self.cursor, sql, params)
                        if sqlparse:
                            print sqlparse.format(raw_sql, reindent=True)
                        else:
                            print raw_sql
                        print
                        print 'Execution time: %.6fs [Database: %s]' % (execution_time, self.db.alias)
                        print

            util.CursorDebugWrapper = PrintQueryWrapper

        global_model_scope = defaultdict(list)
        imported_objects = import_objects(options, self.style, global_model_scope=global_model_scope)

        def run_notebook():
            from django.conf import settings
            from IPython.frontend.html.notebook import notebookapp
            app = notebookapp.NotebookApp.instance()
            ipython_arguments = getattr(settings, 'IPYTHON_ARGUMENTS', ['--ext', 'django_extensions.management.notebook_extension'])
            app.initialize(ipython_arguments)
            app.start()

        def run_plain():
            # Using normal Python shell
            import code
            try:
                # Try activating rlcompleter, because it's handy.
                import readline
            except ImportError:
                pass
            else:
                # We don't have to wrap the following import in a 'try', because
                # we already know 'readline' was imported successfully.
                import rlcompleter
                readline.set_completer(rlcompleter.Completer(imported_objects).complete)
                readline.parse_and_bind("tab:complete")

            # We want to honor both $PYTHONSTARTUP and .pythonrc.py, so follow system
            # conventions and get $PYTHONSTARTUP first then import user.
            if use_pythonrc:
                pythonrc = os.environ.get("PYTHONSTARTUP")
                if pythonrc and os.path.isfile(pythonrc):
                    try:
                        execfile(pythonrc)
                    except NameError:
                        pass
                # This will import .pythonrc.py as a side-effect
                import user  # NOQA
            code.interact(local=imported_objects)

        def run_bpython():
            from bpython import embed
            embed(imported_objects)

        def run_ipython():
            try:
                from IPython import embed
                embed(user_ns=imported_objects)
            except ImportError:
                # IPython < 0.11
                # Explicitly pass an empty list as arguments, because otherwise
                # IPython would use sys.argv from this script.
                # Notebook not supported for IPython < 0.11.
                from IPython.Shell import IPShell
                shell = IPShell(argv=[], user_ns=imported_objects)
                shell.mainloop()

        if use_notebook:
            run_notebook()
        else:
            if auto_reload:
                listen_for_changes(imported_objects, autoreload_path, global_model_scope)

            if use_plain:
                run_plain()
            elif use_ipython:
                run_ipython()
            elif use_bpython:
                run_bpython()
            else:
                for func in (run_ipython, run_bpython, run_plain):
                    try:
                        func()
                    except ImportError:
                        continue
                    else:
                        break
                else:
                    import traceback
                    traceback.print_exc()
                    print self.style.ERROR("Could not load any interactive Python environment.")
