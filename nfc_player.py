from dataclasses import dataclass
import nfc, logging, vlc, os, time, uuid, sys, sqlite3, argparse, mimetypes
from ndef import TextRecord
from ndef import message_decoder
from operator import xor
from tkinter import filedialog, TclError

_VERSION = "1.0"

mimetypes.init()

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

class TerminalFormatter(logging.Formatter):

    grey = "\x1b[38;20m"
    yellow = "\x1b[33;20m"
    red = "\x1b[31;20m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"
    format = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"

    FORMATS = {
        logging.DEBUG: grey + format + reset,
        logging.INFO: grey + format + reset,
        logging.WARNING: yellow + format + reset,
        logging.ERROR: red + format + reset,
        logging.CRITICAL: bold_red + format + reset
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)

class FileFormatter(logging.Formatter):

    fileFormat = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"

    def format(self, record):
        formatter = logging.Formatter(self.fileFormat)
        return formatter.format(record)

logger = logging.getLogger(__name__)

fileHandler = logging.FileHandler("nfcp.log")
fileHandler.setFormatter(FileFormatter())
logger.addHandler(fileHandler)

stdoutHandler = logging.StreamHandler()
stdoutHandler.setFormatter(TerminalFormatter())
logger.addHandler(stdoutHandler)

class NoTagException(Exception):
    """Tag was not present when running operation."""
    def __init__(self, message):
        self.message = message
    def __str__(self):
        return str(self.message)
    
class NoMessageException(Exception):
    """Tag does not have a valid message."""
    def __init__(self, message):
        self.message = message
    def __str__(self):
        return str(self.message)
    
class NoPathException(Exception):
    """Tag does not have a path in the DB."""
    def __init__(self, message):
        self.message = message
    def __str__(self):
        return str(self.message)
    
class BadEntryException(Exception):
    """ Cannot add entry to DB. """
    def __init__(self, message):
        self.message = message
    def __str__(self):
        return str(self.message)

class Database:

    con = None
    cur = None
    db_ver = 1.0
    db_file_name = "NFCPlayer.db"

    def __init__(self):
        logger.debug(f"Initializing database.")
        # Create DB and tables if DB does not exist.
        db_exists = os.path.exists(f"./{self.db_file_name}")
        self.con = sqlite3.connect(self.db_file_name)
        self.cur = self.con.cursor()
        if db_exists == False:
            logger.info("Creating database...")
            self.cur.execute("CREATE TABLE meta(key, value)")
            self.cur.execute(f"INSERT INTO meta VALUES ('version', {self.db_ver})")
            self.cur.execute("CREATE TABLE music(UUID, path)")
            self.con.commit()
            logger.info("Done.")
        else:
            logger.info(f"Found NFCPlayer.db.")
    
    def get_path(self, uuid):
        """ Get a path from a DB entry, else return none. """
        if uuid is None:
            return None
        res = self.cur.execute("SELECT path FROM music WHERE uuid=:uuid", {'uuid': uuid}).fetchone()
        if res == None:
            logger.warning("DB does not contain a path for this tags UUID.")
            return None
        else:
            path = os.path.normpath(res[0])
            logger.info(f"Found {path}")
        return path
    
    def get_all_paths(self):
        """ Get all paths from DB entry, else return none. """
        res = self.cur.execute("SELECT path FROM music").fetchall()
        return res

    def create_entry(self, uuid, path):
        """ Add a uuid and a path to the DB. """
        if (bool(uuid and not uuid.isspace()) == False) or (bool(path and not path.isspace()) == False):
            # Entries is/are empty.
            logger.critical(f"DB entry is not valid: {uuid}, {path}")
            return
        query = 'INSERT INTO music(uuid, path) VALUES(?, ?)'
        values = (uuid, path)
        self.cur.execute(query, values)
        self.con.commit()

    def remove_entry(self, uuid):
        """ Remove an entry using the UUID as key. """
        self.cur.execute("DELETE FROM music WHERE uuid=:uuid", {'uuid': uuid})
        self.con.commit()

class VLC:
    
    MediaPlayer = None
    MediaList = None
    MediaListPlayer = None

    def __init__(self):
        logger.debug(f'Initializing VLC Instance')
        self.Instance = vlc.Instance()
        self.MediaList = self.Instance.media_list_new()
        self.MediaListPlayer = self.Instance.media_list_player_new()
        self.MediaPlayer = self.MediaListPlayer.get_media_player()
        self.MediaPlayer.video_set_key_input(True)

    def __action_timeout(self, action, desired_state, timeout):
        """ Retry action or throw exception. """
        wait = 0
        while (self.MediaListPlayer.get_state() != desired_state) and (wait < timeout):
            action()
            time.sleep(0.5)
            wait += 0.5
        if wait >= timeout:
            if desired_state == vlc.State.Playing:
                raise Exception("Error when playing the playlist.")
            if desired_state == vlc.State.Paused:
                raise Exception("Error when pausing the playlist.")
            else:
                raise Exception("Action could not be completed.")

    def clear_playlist(self):
        self.MediaListPlayer.stop()
        self.MediaList = self.Instance.media_list_new()

    def add_track_mrls(self, track_list):
        self.clear_playlist()
        for track in track_list:
            logger.debug(f"Adding song {track}")
            media = self.Instance.media_new(track)
            media.parse()
            self.MediaList.add_media(media)
        self.MediaListPlayer.set_media_list(self.MediaList)
        self.MediaListPlayer.set_playback_mode(vlc.PlaybackMode.default)
        logger.debug(f"Added {len(track_list)} songs to playlist")

    def play(self):
        if self.MediaList.count() > 0:
            try:
                self.__action_timeout(self.MediaListPlayer.play, vlc.State.Playing, 3)
                logger.info(f"Playing.")
            except:
                logger.warning("Failed to play media player.")
                self.MediaPlayer.stop()

        else:
            logger.info(f"Can't play, no media in playlist.")

    def pause(self):
        if self.MediaListPlayer.get_state() == vlc.State.Playing:
            try:
                self.__action_timeout(self.MediaListPlayer.pause, vlc.State.Paused, 3)
                logger.info(f"Paused playlist")
            except:
                logger.warning("Failed to pause media player.")
                self.MediaPlayer.stop()

    def stop(self):
        if self.MediaListPlayer is not None:
            try:
                self.__action_timeout(self.MediaListPlayer.stop, vlc.State.Stopped, 3)
                logger.info(f"Stopped")
            except:
                logger.warning("Failed to stop media player.")
                self.MediaPlayer.stop()

    def next(self):
        self.MediaListPlayer.next()

    def previous(self):
        self.MediaListPlayer.previous()

@dataclass
class Tag:
    Tag = None
    UUID = None
    Path = None

    def __init__(self, Tag, UUID: str = "", Path: str = ""):
        self.Tag = Tag
        self.UUID = UUID
        self.Path = Path


class NFCPlayer:

    DB = None
    VLC = None
    clf = None
    tag = None
    prev_uuid = -1
    reprint = True
    default_directory = False
    _GUI_MODE = None
    _WRITE_MODE = False
    _BATCH_MODE = False
    _CHECK_MODE = False
    batch_dirs = []
    halt = False

    standby_options = None
    write_mode_options = None
    
    scan_ascii_msg = "  ___  ___   _   _  _   _____ _   ___ \n / __|/ __| /_\\ | \\| | |_   _/_\\ / __|\n \\__ \\ (__ / _ \\| .` |   | |/ _ \\ (_ |\n |___/\\___/_/ \\_\\_|\\_|   |_/_/ \\_\\___|"
    success_ascii_msg = " ___ _   _  ___ ___ ___  ___ ___ \n/ __| | | |/ __/ __/ _ \\/ __/ __|\n\\__ \\ |_| | (_| (_|  __/\\__ \\__ \\\n|___/\\__,_|\\___\\___\\___||___/___/"
    qs_ascii_msg = " ________   ________           ________  ________  ________  ________           _________  ________  ________     \n|\\   __  \\ |\\   ____\\         |\\   ____\\|\\   ____\\|\\   __  \\|\\   ___  \\        |\\___   ___\\\\   __  \\|\\   ____\\    \n\\ \\  \\|\\  \\\\ \\  \\___|_        \\ \\  \\___|\\ \\  \\___|\\ \\  \\|\\  \\ \\  \\\\ \\  \\       \\|___ \\  \\_\\ \\  \\|\\  \\ \\  \\___|    \n \\ \\  \\\\\\  \\\\ \\_____  \\        \\ \\_____  \\ \\  \\    \\ \\   __  \\ \\  \\\\ \\  \\           \\ \\  \\ \\ \\   __  \\ \\  \\  ___  \n  \\ \\  \\\\\\  \\\\|____|\\  \\        \\|____|\\  \\ \\  \\____\\ \\  \\ \\  \\ \\  \\\\ \\  \\           \\ \\  \\ \\ \\  \\ \\  \\ \\  \\|\\  \\ \n   \\ \\_____  \\ ____\\_\\  \\         ____\\_\\  \\ \\_______\\ \\__\\ \\__\\ \\__\\\\ \\__\\           \\ \\__\\ \\ \\__\\ \\__\\ \\_______\\\n    \\|___| \\__\\\\_________\\       |\\_________\\|_______|\\|__|\\|__|\\|__| \\|__|            \\|__|  \\|__|\\|__|\\|_______|\n          \\|__\\|_________|       \\|_________|                                                                     \n                                                                                                                  \n"

    # "Private" Functions
    def __init__(self, args):
        logger.info(f"Initializing NFC Player.")

        location = args["location"]
        self.default_directory = args["default_directory"]
        self._GUI_MODE = not args["terminal_only"]
        self._WRITE_MODE = args["write_mode"]
        if args["batch_mode_dir"] is not None:
            self._WRITE_MODE = True
            self._BATCH_MODE = True
            print("Started in Batch Write Mode.")
            logger.info(f"Path given: {args['batch_mode_dir']}")
            self.load_batch_directories_file(args["batch_mode_dir"])
            if len(self.batch_dirs) == 0:
                self.halt = True
                logger.error(f"No valid directories found in batch write file. Nothing to do.")
                return
    
        self._CHECK_MODE = args["check_paths"]

        self.standby_options = {
            'on-startup': self.on_startup,
            'on-connect': self.read_and_play,
            'on-release': self.on_release,
            'interval': 0.5,
            'beep-on-connect': False
        }

        self.write_mode_options = {
            'on-startup': self.on_startup,
            'on-connect': self.read_and_assign,
            'on-release': self.on_release,
            'interval': 0.5,
            'beep-on-connect': False
        }
        
        if self._CHECK_MODE == False:
            logger.debug(f"Looking for NFC card reader at '{location}'")
            self.clf = nfc.ContactlessFrontend()

            tries = 0
            attempt = self.clf.open(location)
            while (attempt == False) and (tries < 6):
                logger.debug(f"Looking for NFC card reader at '{location}'")
                attempt = self.clf.open(location)
                tries += 1

            if attempt == False:
                logger.critical(f"Did not find NFC Card Reader at path '{location}'")
                print("No reader found. Please give another location for the nfc card reader with -l (--location) flag.")
                self.quit_player()
                self.halt = True
                return
            logger.info("Connected to NFC reader.")
        
        self.set_app_display_mode()

        self.VLC = VLC()
        self.__set_events()
        self.DB = Database()
        logger.debug(f"NFC Player initialized.")

    def __set_events(self):
        MP_events = self.VLC.MediaPlayer.event_manager()
        MP_events.event_attach(vlc.EventType.MediaPlayerPlaying, self.print_state_and_curr_track)
        MP_events.event_attach(vlc.EventType.MediaPlayerPaused, self.print_state_and_curr_track)
        MP_events.event_attach(vlc.EventType.MediaPlayerMediaChanged , self.print_state_and_curr_track)
        MP_events.event_attach(vlc.EventType.MediaPlayerStopped,self.print_state_and_curr_track)

    def delete_last_line(self):
        sys.stdout.write('\x1b[2K')
        sys.stdout.flush()

    def print_state_and_curr_track(self, event=""):
        logger.debug(f"MediaListPlayerState: {self.VLC.MediaListPlayer.get_state()}")
        state = ""
        if stdoutHandler.level not in [logging.INFO, logging.DEBUG]:
            self.delete_last_line()
            end = "\r"
        else:
            end = "\n"

        match self.VLC.MediaListPlayer.get_state():
            case vlc.State.Playing:
                state = "▶"
            case vlc.State.Paused:
                state = "⏸"
            case vlc.State.Stopped | vlc.State.Ended:
                state = "■"
            case vlc.State.NothingSpecial:
                state = "-"
            case _:
                state = "?"
        media = self.VLC.MediaPlayer.get_media()
        if media is None:
            print(state, end=end)
        else:
            print(f"{state} {media.get_meta(0)} - {media.get_meta(1)}", end=end)

    def quit_player(self):
        print()
        print("Quitting...")
        logger.debug("Closing connection to NFC Card Reader")
        if self.clf is not None:
            self.clf.close()
        logger.debug("Releasing VLC Instance")
        if self.VLC is not None:
            self.VLC.Instance.release()

    def on_startup(self, targets):
        for target in targets:
            target.sensf_req = bytearray.fromhex("0012FC0000")
        return targets

    def set_app_display_mode(self):
        if self._GUI_MODE is False:
            logger.info(f"Terminal only mode.")
        elif self._GUI_MODE is None:
            logger.info(f"Checking for display...")
            if 'DISPLAY' in os.environ:
                self._GUI_MODE = True
            else:
                self._GUI_MODE = False

    # Main functionalities
    def on_release(self, event=""):
        self.VLC.pause()

    def get_uuid_from_tag(self):
        if (self.tag is None) or (self.tag.Tag.is_present == False):
            return None
        if (self.tag.Tag.ndef is None):
            if self._WRITE_MODE:
                logger.info(f"Tag is empty.")
                return None
            else:
                logger.info("Tag doesn't have a valid ID, please try another tag.")
        
        logger.info(f"Getting tag UUID")
        try:
            l = message_decoder(self.tag.Tag.ndef.octets)
        except AttributeError:
            return None
        
        if len(list(l)) > 0:
            uuid = list(message_decoder(self.tag.Tag.ndef.octets))[0].text
            logger.debug(f"Tag UUID: {uuid}")
            return uuid
        else:
            return None

    def play_loop(self):
        logger.debug(f"Going into standby mode.")
        print(f"NFC Music Player {_VERSION}, ready to play.")
        while True:
            try:
                logger.info("Waiting for tag...")
                if self.clf.connect(rdwr=self.standby_options) == False:
                    break
            except KeyboardInterrupt:
                print()
                break
        self.quit_player()
        return

    def read_and_play(self, tag):
        logger.info("Connected to tag.")
        logger.debug(f"Tag: {tag}")
        self.tag = Tag(tag)

        self.tag.UUID = self.get_uuid_from_tag()
        if (self.tag.UUID is None):
            self.tag = None
            print("Tag is empty.")
            return True
        
        self.tag.Path = self.DB.get_path(self.tag.UUID)
        if (self.tag.Path is None):
            self.tag = None
            print("Tag has no media folder assigned to it.")
            return True
        
        if self.tag.UUID != self.prev_uuid:
            self.add_media_to_playlist(self.tag.Path)
            self.prev_uuid = self.tag.UUID
        self.VLC.play()
        return True

    def write_loop(self):
        print(f"NFC Music Player {_VERSION}, ready to write.")
        
        while True and not xor(bool(self._BATCH_MODE), bool(len(self.batch_dirs) > 0)):
            logger.debug("Entering write_loop")
            # Print messages only if coming for a new tag.
            if self.reprint:
                self.reprint = False
                if self._BATCH_MODE:
                    print(f"Assigning {self.batch_dirs[-1]}")
                    print(self.scan_ascii_msg)
                    print(f"Or skip with Ctrl+c")
                else:
                    print()
                    print(self.scan_ascii_msg)
            try:
                self.halt = False
                # There are 3 control flow paths to trigger the quiting of the program.
                # One: clf.connect() returns false from KeyboardInterrupt while waiting for tag
                # Two: During tag reading (in the on-connect callback) a KeyboardInterrupt sets 'halt' to true and returns
                # Three: If in BATCH_MODE, the KeyboardInterrupt first trigger skipping of current batch_dir, second KeyboardInterrupt triggers breaking loop
                # -
                # This is because the on-connect callback cannot trigger clf.connect to return false, so the 'halt' variable is needed to detect whats going on in the callback.
                if (self.clf.connect(rdwr=self.write_mode_options) == False) or self.halt:
                    if self._BATCH_MODE and len(self.batch_dirs) > 1:
                        ans = ""
                        while ans.upper() not in ["Y","N"]:
                            ans = input(f"\nWould you like to skip writing to {self.batch_dirs[-1]} Y/N:")
                        if (ans.upper() == "Y"):
                            self.batch_dirs.pop(-1)
                            self.reprint = True
                        continue
                    else:
                        break
            except KeyboardInterrupt:
                break
            except nfc.tag.TagCommandError as err:
                self.DB.remove_entry(self.tag.UUID)
                logger.error(f"NDEF write failed: {str(err)}")
                logger.error(f"You probably removed the tag before its UUID could be written.")
        self.quit_player()

    def read_and_assign(self, tag):
        try:
            self.reprint = True
            print("Checking")
            logger.info("Connected to tag.")
            logger.debug(f"Tag: {tag}")
            self.tag = Tag(tag)
            self.tag.UUID = self.get_uuid_from_tag()
            self.tag.Path = self.DB.get_path(self.tag.UUID)

            if (self.tag.Path is not None):
                print(f"Tag is already pointing to: {self.tag.Path}")
                ans = ""
                while ans.upper() not in ["Y","N"]:
                    ans = input("Do you want to overwrite this tag Y/N: ")
                if (ans.upper() == "N"):
                    print(f"Please remove tag.")
                    return True
                else:
                    self.DB.remove_entry(self.tag.UUID)
            
            # Get random UUID.
            self.tag.UUID = "NFCMP_" + str(uuid.uuid4())
            
            # Prompt user to select/input a media directory.
            if self._BATCH_MODE:
                self.tag.Path = self.batch_dirs.pop()
            else:
                logger.info("Prompting user to select/input media directory.")
                self.tag.Path = None
                while self.check_if_directory_contains_media(self.tag.Path) is False:
                    self.tag.Path = self.select_media_directory()
            
            # Try to write the uuid to the tag.
            self.tag.Tag.ndef.records = [TextRecord(self.tag.UUID)]

            # Try to create DB entry.
            self.DB.create_entry(self.tag.UUID, self.tag.Path)

            # Check
            if ( self.tag.UUID == list(message_decoder(self.tag.Tag.ndef.octets))[0].text ) and \
                ( self.DB.get_path(self.tag.UUID) == self.tag.Path ):
                logger.info(f"Succesfully added entry to DB: \n\t{self.tag.UUID},\n\t{self.tag.Path}")
                print(self.success_ascii_msg)
                print("Succesfully assigned media to tag.")
            else:
                print("Writing failed, please try again.")
            print("Waiting for tag to be removed...")
            return True
        except KeyboardInterrupt:
            # KeyboardInterrupt should immediately return from the on-connect callback and into the main thread, 
            # while also signaling that
            self.halt = True
            return False

    # File ops
    def add_media_to_playlist(self, directory):
        if os.path.isfile(directory):
            self.VLC.add_track_mrls(directory)
        else:
            track_list = []
            for file_name in sorted(os.listdir(directory)):
                file = os.path.join(directory, file_name)
                if self.is_audio_track(file):
                    track_list.append(file)
            self.VLC.add_track_mrls(track_list)
            logger.info(f"Added {len(track_list)} songs to playlist")

    def load_batch_directories_file(self, file):
        f = open(os.path.realpath(os.path.expanduser(file)), 'r')
        logger.debug(os.path.realpath(os.path.expanduser(file)))
        for line in f:
            if os.path.exists(os.path.expanduser(line.strip())):
                self.batch_dirs.append(os.path.expanduser(line.strip()))
        logger.info(f"Found {len(self.batch_dirs)} paths")

    def check_paths(self):
        paths = self.DB.get_all_paths()
        for path in paths:
            if self.check_if_directory_contains_media(path[0]) == False:
                print(path[0])
    
    def select_media_directory(self):
        directory = None
        if self._GUI_MODE:
            logger.info("Opening file dialog...")
        
        while directory is None:
            if self._GUI_MODE:
                try:
                    directory = filedialog.askdirectory( title="Select a media folder\t\t\t\t\t\t\t\t\t\t\t\t", initialdir=self.default_directory)
                    # When window is closed without selecting it generates a tuple.
                    # Use that as a signal to determine if user wants to end the program.
                    if type(directory) == tuple:
                        ans = ""
                        while ans.upper() not in ["Y","N"]:
                            ans = input("End the program: Y/N? ")
                        if (ans.upper() == "Y"):
                            raise KeyboardInterrupt
                        directory = None
                        continue
                except TypeError:
                    directory = None
                    continue
                except (TclError, RuntimeError) as e:
                    self._GUI_MODE = False
                    logger.warning(f"Could not open file dialog. Falling back to terminal only mode.")
                    logger.debug(f"{e}")
                    directory = None
                    continue
            else:
                directory = input("Input path to media: ")
                if directory == "\n" or directory == "":
                    directory = None
                    continue
        directory = os.path.normpath(os.path.expanduser(directory.strip().strip('\'')))
        logger.debug(f"Selected {directory}")
        return directory

    def check_if_directory_contains_media(self, path):
        if path is None:
            return False
        if os.path.exists(path) is False:
            if self._CHECK_MODE == False:
                logger.warning(f"Not a valid path.")
            return False

        tracks = 0
        for file_name in sorted(os.listdir(path)):
            file = os.path.join(path, file_name)
            if self.is_audio_track(file):
                tracks += 1
        if tracks > 0:
            if self._CHECK_MODE == False:
                print(f"Found {tracks} tracks in directory {path}.")
            return True
        else:
            if self._CHECK_MODE == False:
                logger.warning(f"No tracks found in directory.")
        return False

    def is_playlist(self, file):
        ext =  os.path.splitext(file)[1]
        if(ext.upper() in [".M3U", ".ASX", ".XSPF", ".B4S", ".CUE"]):
            return True
        return False

    def is_audio_track(self, file):
        if os.path.isfile(file):
            mime_type = mimetypes.guess_type(file)[0]
            if (mime_type is not None) and (self.is_playlist(file) == False) and mime_type.startswith("audio"):
                logger.debug(f"{file} is an audio track.")
                return True
            else:
                logger.debug(f"{file} is not an audio track.")

parser = argparse.ArgumentParser(
                    prog='NFC Player',
                    description='Physical interface for a digital library.')

parser.add_argument('-l', '--location',
                    help='Path to NFC reader. Defaults to \'usb\'.', default='usb')

parser.add_argument('-w', '--write_mode',
                    help='Write mode. Allows you to write to a tag. Press CTRL+C to quit.',
                    action='store_true')

parser.add_argument('-f', '--default_directory',
                    help='Path to open file dialog in.')

parser.add_argument('-t', '--terminal_only',
                    help='No GUI elements (no file dialog).',
                    action='store_true')

parser.add_argument('-b', '--batch_mode',
                    help='Write tags sequentially from a file containing a list of directories.',
                    default=None, dest="batch_mode_dir")

parser.add_argument('-c', '--check_paths',
                    help='Check if paths in the database are valid, real and contain media. Write invalid paths to stdout.',
                    action='store_true')

parser.add_argument('-d', '--debug',
                    help='For debugging output.',
                    action="store_const", dest="log_level", const=logging.DEBUG, default=logging.WARNING)

parser.add_argument('-v', '--verbose',
                    help='For verbose output.',
                    action="store_const", dest="log_level", const=logging.INFO)

args = parser.parse_args()

    
def main():
    # Set whole logger to lowest level to enable different log levenls.
    # And fileHandler to verbose unless debug is enabled.
    logger.setLevel(level=logging.DEBUG)
    if args.log_level == logging.DEBUG:
        fileHandler.setLevel(level=logging.DEBUG)
    else:
        fileHandler.setLevel(level=logging.INFO)
    stdoutHandler.setLevel(level=args.log_level)
    clear_screen()
    logger.info('Started app')

    NFC_player = NFCPlayer(vars(args))
    
    if NFC_player.halt:
        pass
    elif (args.write_mode) or (args.batch_mode_dir is not None):
        NFC_player.write_loop()
    elif args.check_paths:
        NFC_player.check_paths()
    else:
        NFC_player.play_loop()
    
    print("Bye")
    logger.info("Done")

if __name__ == '__main__': 
    sys.exit(main())
