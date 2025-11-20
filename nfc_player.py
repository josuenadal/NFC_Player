from pathlib import Path
import nfc
import logging
import vlc
import os
import time
import uuid
import sys
from ndef import TextRecord
from ndef import message_decoder
from operator import xor
from tkinter import filedialog
from tkinter import TclError
import sqlite3
import argparse
import mimetypes

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
        res = self.cur.execute("SELECT path FROM music WHERE uuid=:uuid", {'uuid': uuid}).fetchone()
        if res == None:
            raise NoPathException("DB does not contain a path for this tags UUID.")
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
            raise BadEntryException("Missing UUID or path for DB entry.")
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

class NFCPlayer:

    DB = None
    VLC = None
    clf = None
    tag = None
    prev_uuid = -1
    default_directory = False
    _GUI_MODE = None
    _WRITE_MODE = False
    _BATCH_MODE = False
    _CHECK_MODE = False
    batch_dirs = []

    scan_ascii_msg = "  ___  ___   _   _  _   _____ _   ___ \n / __|/ __| /_\\ | \\| | |_   _/_\\ / __|\n \\__ \\ (__ / _ \\| .` |   | |/ _ \\ (_ |\n |___/\\___/_/ \\_\\_|\\_|   |_/_/ \\_\\___|"
    success_ascii_msg = " ___ _   _  ___ ___ ___  ___ ___ \n/ __| | | |/ __/ __/ _ \\/ __/ __|\n\\__ \\ |_| | (_| (_|  __/\\__ \\__ \\\n|___/\\__,_|\\___\\___\\___||___/___/"
    qs_ascii_msg = " ________   ________           ________  ________  ________  ________           _________  ________  ________     \n|\\   __  \\ |\\   ____\\         |\\   ____\\|\\   ____\\|\\   __  \\|\\   ___  \\        |\\___   ___\\\\   __  \\|\\   ____\\    \n\\ \\  \\|\\  \\\\ \\  \\___|_        \\ \\  \\___|\\ \\  \\___|\\ \\  \\|\\  \\ \\  \\\\ \\  \\       \\|___ \\  \\_\\ \\  \\|\\  \\ \\  \\___|    \n \\ \\  \\\\\\  \\\\ \\_____  \\        \\ \\_____  \\ \\  \\    \\ \\   __  \\ \\  \\\\ \\  \\           \\ \\  \\ \\ \\   __  \\ \\  \\  ___  \n  \\ \\  \\\\\\  \\\\|____|\\  \\        \\|____|\\  \\ \\  \\____\\ \\  \\ \\  \\ \\  \\\\ \\  \\           \\ \\  \\ \\ \\  \\ \\  \\ \\  \\|\\  \\ \n   \\ \\_____  \\ ____\\_\\  \\         ____\\_\\  \\ \\_______\\ \\__\\ \\__\\ \\__\\\\ \\__\\           \\ \\__\\ \\ \\__\\ \\__\\ \\_______\\\n    \\|___| \\__\\\\_________\\       |\\_________\\|_______|\\|__|\\|__|\\|__| \\|__|            \\|__|  \\|__|\\|__|\\|_______|\n          \\|__\\|_________|       \\|_________|                                                                     \n                                                                                                                  \n"

    def __init__(self, location, display_mode, check_paths_mode = False):
        logger.info(f"Initializing NFC Player.")
        logger.debug(f"Looking for NFC card reader at '{location}'")

        self._CHECK_MODE = check_paths_mode
        if self._CHECK_MODE == False:
            try:
                self.clf = nfc.ContactlessFrontend(location)
                logger.info("Connected to NFC reader.")
            except OSError:
                logger.error(f"No reader found at '{location}', if one is present it may be occupied.")
                logger.info(f"Quitting...")
                quit()
        
        logger.debug(f"Display mode set to {display_mode}")
        self._GUI_MODE = display_mode
        self.set_app_display_mode()

        self.VLC = VLC()
        self.__set_events()
        self.DB = Database()
        logger.debug(f"NFC Player initialized.")
    
    def __set_events(self):
        # MLP_events = self.VLC.MediaListPlayer.event_manager()
        MP_events = self.VLC.MediaPlayer.event_manager()
        MP_events.event_attach(vlc.EventType.MediaPlayerPlaying, self.print_state_and_curr_track)
        MP_events.event_attach(vlc.EventType.MediaPlayerPaused, self.print_state_and_curr_track)
        MP_events.event_attach(vlc.EventType.MediaPlayerMediaChanged , self.print_state_and_curr_track)
    
    def wait_for_tag(self):
        rdwr_options = {
            'on-startup': self.on_startup,
            'on-connect': self.on_connect,
        }
        tag = None
        logger.info("Waiting for tag...")
        tag = self.clf.connect(rdwr=rdwr_options)
        if tag is False:
            self.tag = None
            raise KeyboardInterrupt()
        else:
            self.tag = tag

    def on_connect(self, tag):
        logger.info("Connected to tag.")
        logger.debug(f"Tag: {tag}")

    def on_startup(self, targets):
        for target in targets:
            target.sensf_req = bytearray.fromhex("0012FC0000")
        return targets
    
    def is_tag_different_from_prev(self, uuid):
        ans = (uuid != self.prev_uuid)
        self.prev_uuid = uuid
        return ans
    
    def standby(self):
        logger.debug(f"Going into standby mode.")
        print(f"NFC Music Player {_VERSION}, ready to play.")
        while True:
            try:
                self.wait_for_tag()
                uuid = self.get_uuid_from_tag()
                path = self.DB.get_path(uuid)
                if self.is_tag_different_from_prev(uuid):
                    # If tag is not the previous tag, clear playlist and add new music.
                    self.add_tracks_to_playlist(path)
                else:
                    logger.info("Continuing.")
                self.VLC.play()

                while (self.VLC.MediaListPlayer.get_state() == vlc.State.Playing) and (self.tag.is_present):
                    time.sleep(0.25)
                
                self.VLC.pause()
                
            except NoMessageException as NME:
                logger.error(f"{NME}")
                time.sleep(2)
                continue
            except NoTagException as NTE:
                logger.error(f"{NTE}")
                continue
            except NoPathException as err:
                logger.error(f"No path exception: {err}")
                time.sleep(2)
                logger.info(f"Please remove the tag.")
                self.wait_for_tag_to_be_removed()
                continue
            except FileNotFoundError:
                logger.error(f"The path was not located, try another tag or rewrite this tag with a valid media path.")
                self.wait_for_tag_to_be_removed()
                continue
            except KeyboardInterrupt:
                self.VLC.MediaPlayer.stop()
                print("\n")
                logger.info(f"Quitting...")
                break
        self.VLC.stop()

    def print_state_and_curr_track(self, event=""):
        logger.debug(f"MediaListPlayerState: {self.VLC.MediaListPlayer.get_state()}")
        state = ""
        if stdoutHandler.level not in [logging.INFO, logging.DEBUG]:
            self.delete_last_line()
            end = "\r"
        else:
            end = "\n"
        if self.VLC.MediaListPlayer.get_state() == vlc.State.Playing:
            state = "▶"
        elif self.VLC.MediaListPlayer.get_state() == vlc.State.Paused:
            state = "⏸"
        elif self.VLC.MediaListPlayer.get_state() == vlc.State.Stopped:
            state = "■"
        media = self.VLC.MediaPlayer.get_media()
        if media is None:
            print(state, end=end)
        else:
            print(f"{state} {media.get_meta(0)} - {media.get_meta(1)}", end=end)
            
    def delete_last_line(self):
        sys.stdout.write('\x1b[2K')
        sys.stdout.flush()

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

    def add_tracks_to_playlist(self, directory):
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

    def get_uuid_from_tag(self):
        if (self.tag is None) or (self.tag.is_present == False):
            raise NoTagException("Tag was not present when looking up UUID.")
        if (self.tag.ndef is None):
            if self._WRITE_MODE:
                logger.info(f"Tag is empty.")
                return None
            else:
                raise NoMessageException("Tag doesn't have a valid ID, please try another tag.")
        
        logger.info(f"Getting tag UUID")
        l = message_decoder(self.tag.ndef.octets)
        if len(list(l)) > 0:
            uuid = list(message_decoder(self.tag.ndef.octets))[0].text
            logger.debug(f"Tag UUID: {uuid}")
            return uuid
        else:
            return None
        
    def wait_for_tag_to_be_removed(self):
        logger.info(f"Waiting for tag to be removed.")
        while self.tag.is_present:
            time.sleep(0.35)

    def set_app_display_mode(self):
        if self._GUI_MODE is False:
            logger.info(f"Terminal only mode.")
        elif self._GUI_MODE is None:
            logger.info(f"Checking for display...")
            if 'DISPLAY' in os.environ:
                self._GUI_MODE = True
            else:
                self._GUI_MODE = False

    def select_media_directory(self):
        directory = None
        if self._GUI_MODE:
            logger.info("Opening file dialog...")
        
        while directory is None:
            if self._GUI_MODE:
                try:
                    directory = filedialog.askdirectory(initialdir= self.default_directory)
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
    
    def write_tags(self, default_directory):
        print(f"NFC Music Player {_VERSION}, ready to write.")
        self._WRITE_MODE = True        
        self.default_directory = default_directory
        
        while True and not xor(bool(self._BATCH_MODE), bool(len(self.batch_dirs) > 0)):
            try:
                if self._BATCH_MODE:
                    print(f"Assigning {self.batch_dirs[-1]}")
                    print(self.scan_ascii_msg)
                    print(f"Or skip with Ctrl+c")
                else:
                    print(self.scan_ascii_msg)

                self.wait_for_tag()
                uuid_key = self.get_uuid_from_tag()
                
                directory = None
                if uuid_key is not None:
                    try:
                        directory = self.DB.get_path(uuid_key)
                    except NoPathException:
                        directory = None

                if (directory is not None):
                    print(f"Tag is already pointing to: {directory}")
                    ans = ""
                    while ans.upper() not in ["Y","N"]:
                        ans = input("Do you want to overwrite this tag Y/N: ")
                    if (ans.upper() == "N"):
                        print(f"Please remove tag.")
                        self.wait_for_tag_to_be_removed()
                        continue
                    else:
                        self.DB.remove_entry(uuid_key)

                # Get random UUID.
                uuid_key = "NFCMP_" + str(uuid.uuid4())
                
                # Prompt user to select/input a media directory.
                if self._BATCH_MODE is False:
                    logger.info("Prompting user to select/input media directory.")
                    directory = None
                    while self.check_if_directory_contains_media(directory) is False:
                        directory = self.select_media_directory()
                else:
                    directory = self.batch_dirs.pop()


                
                # Try to write the uuid to the tag.
                self.tag.ndef.records = [TextRecord(uuid_key)]

                # Try to create DB entry.
                self.DB.create_entry(uuid_key, directory)
                logger.info(f"Succesfully added entry to DB: \n\t{uuid_key},\n\t{directory}")

                print(self.success_ascii_msg)
                print("Succesfully assigned media to tag.")

                # Done, wait for tag to be removed.
                print("Waiting for tag to be removed...")
                self.wait_for_tag_to_be_removed()

            except NoTagException as NTE:
                logger.error(f"{NTE}")
                continue
            except nfc.tag.TagCommandError as err:
                self.DB.remove_entry(uuid_key)
                logger.error(f"NDEF write failed: {str(err)}")
                logger.error(f"You probably removed the tag before its UUID could be written.")
            except KeyboardInterrupt:
                if self._BATCH_MODE:
                    try:
                        ans = ""
                        while ans.upper() not in ["Y","N"]:
                            ans = input(f"Would you like to skip writing to {self.batch_dirs[-1]} Y/N:")
                        if (ans.upper() == "Y"):
                            self.batch_dirs.pop()
                            print("\n")
                            continue
                        else:
                            continue
                    except KeyboardInterrupt:
                        pass
                print("\n")
                print("Quitting...")
                logger.info(f"Quitting...")
                self.VLC.stop()
                return
            except BadEntryException as err:
                logger.critical(f"Adding entry to DB failed: {str(err)}")
    
    def init_batch_mode_mode(self, directories_file):
        print("Started in Batch Write Mode.")
        logger.info(f"Path given: {directories_file}")
        self. _BATCH_MODE = True
        self.load_batch_directories_file(directories_file)
        if self.batch_dirs == 0:
            logger.error(f"No valid directories found in batch write file.")
            quit()

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

parser = argparse.ArgumentParser(
                    prog='NFC Player',
                    description='Physical interface for a digital library.')

parser.add_argument('-l', '--location',
                    help='Path to NFC reader. Defaults to \'usb\'.', default='usb')

parser.add_argument('-w', '--write',
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
                    help='Check if paths in the database are valid, real and contain media. Write them to stdout.',
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
    NFC_player = NFCPlayer(args.location, not args.terminal_only, args.check_paths)

    if args.write:
        NFC_player.write_tags(args.default_directory)
        NFC_player.standby()
    elif args.batch_mode_dir is not None:
        NFC_player.init_batch_mode_mode(args.batch_mode_dir)
        NFC_player.write_tags(args.default_directory)
        NFC_player.standby()
    elif args.check_paths:
        NFC_player.check_paths()
    else:
        NFC_player.standby()
    
    print("Bye")
    logger.info("Done")

if __name__ == '__main__': 
    sys.exit(main())
