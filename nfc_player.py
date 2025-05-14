import nfc
import logging
import vlc
import os
import time
import uuid
import sys
from ndef import TextRecord
from ndef import message_decoder
from tkinter import filedialog
import sqlite3
import argparse
import mimetypes

mimetypes.init()

logger = logging.getLogger(__name__)
stdoutHandler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s | %(name)s | %(levelname)s | %(message)s')
stdoutHandler.setFormatter(formatter)
logger.addHandler(stdoutHandler)
logger.setLevel(logging.DEBUG)

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

class NoTagException(Exception):
    """Tag was not present when running operation."""
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
    db_name = "NFCPlayer.db"

    def __init__(self):
        # Create DB and tables if DB does not exist.
        db_exists = os.path.exists(f"./{self.db_name}")
        self.con = sqlite3.connect(self.db_name)
        self.cur = self.con.cursor()
        if db_exists == False:
            logger.info("Creating database...")
            self.cur.execute("CREATE TABLE meta(key, value)")
            self.cur.execute(f"INSERT INTO meta VALUES ('version', {self.db_ver})")
            self.cur.execute("CREATE TABLE music(UUID, path)")
            self.con.commit()
            logger.info("Done.")

    def is_not_blank(self, s):
        return bool(s and not s.isspace())
    
    def get_path(self, uuid):
        """ Get a path from a DB entry, else return none. """
        res = self.cur.execute("SELECT path FROM music WHERE uuid=:uuid", {'uuid': uuid}).fetchone()
        if res == None:
            raise NoPathException("DB does not contain a path for this tags UUID.")
        else:
            path = os.path.normpath(res[0])
            logger.info(f"Found {path}")
        return path
    
    def create_entry(self, uuid, path):
        """ Add a uuid and a path to the DB. """
        if (self.is_not_blank(uuid) == False) or (self.is_not_blank(path) == False):
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
    media_list = None
    list_player = None

    def __init__(self):
        self.player = vlc.Instance()

    def action_timeout(self, action, desired_state, timeout):
        """ Retry action or throw exception. """
        wait = 0
        while (self.list_player.get_state() != desired_state) and (wait < timeout):
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
            
    def add_playlist(self):
        self.media_list = self.player.media_list_new()
        self.list_player = self.player.media_list_player_new()
        self.list_player.set_media_list(self.media_list)

    def clear_playlist(self):
        self.add_playlist()

    def add_song(self, file):
        if self.media_list is None:
            self.add_playlist()
        self.media_list.add_media(self.player.media_new(file))
        logger.info(f"Added song to playlist: {os.path.basename(file)}")

    def play(self):
        if self.media_list.count() > 0:
            self.action_timeout(self.list_player.play, vlc.State.Playing, 3)
            logger.info(f"Playing.")
        else:
            logger.info(f"Can't play, no media in playlist.")

    def next(self):
        self.list_player.next()

    def pause(self):
        if self.list_player.get_state() == vlc.State.Playing:
            self.action_timeout(self.list_player.pause, vlc.State.Paused, 3)
            logger.info(f"Paused playlist")

    def previous(self):
        self.list_player.previous()

    def stop(self):
        if self.list_player is not None:
            self.action_timeout(self.list_player.stop, vlc.State.Stopped, 3)
            logger.info(f"Stopped")

class NFC_Player:

    DB = None
    clf = None
    tag = None
    player = None
    prev_uuid = -1
    default_directory = False

    scan_ascii_msg = "  ___  ___   _   _  _   _____ _   ___ \n / __|/ __| /_\\ | \\| | |_   _/_\\ / __|\n \\__ \\ (__ / _ \\| .` |   | |/ _ \\ (_ |\n |___/\\___/_/ \\_\\_|\\_|   |_/_/ \\_\\___|"

    def __init__(self):
        try:
            self.clf = nfc.ContactlessFrontend('usb')
            logger.info("Connected to USB NFC reader.")
        except OSError:
            logger.error("No reader found, if one is present it may be occupied.")
            quit()
        self.player = VLC()
        self.DB = Database()
    
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
        logger.debug(f"Connected to tag {tag}")

    def on_startup(self, targets):
        for target in targets:
            target.sensf_req = bytearray.fromhex("0012FC0000")
        return targets
    
    def is_new_tag(self, uuid):
        ans = (uuid != self.prev_uuid)
        self.prev_uuid = uuid
        return ans
    
    def standby(self):
        while True:
            try:
                self.wait_for_tag()
                uuid = self.get_uuid_from_tag()
                path = self.DB.get_path(uuid)
                if self.is_new_tag(uuid):
                    # If tag is not the previous tag, clear playlist and add new music.
                    self.add_folder_to_playlist(path)
                else:
                    logger.info("Continuing.")
                self.player.play()
                while (self.player.list_player.get_state() == vlc.State.Playing) and (self.tag.is_present):
                    time.sleep(0.5)
                self.player.pause()
                self.wait_for_tag_to_be_removed()
            except NoTagException as NTE:
                logger.error(f"{NTE}")
                continue
            except NoPathException as err:
                logger.error(f"No path exception: {err}")
                time.sleep(5)
                continue
            except KeyboardInterrupt:
                logger.info(f"Quitting...")
                break
        self.player.stop()

    def is_playlist(self, file):
        ext =  os.path.splitext(file)[1]
        if(ext.upper() in [".M3U", ".ASX", ".XSPF", ".B4S", ".CUE"]):
            return True
        return False

    def get_playlist(self, path):
        for file in sorted(os.listdir(path)):
            if self.is_playlist(file):
                return os.path.normpath(path + "/" + file)
        return None

    def add_folder_to_playlist(self, path):
        self.player.clear_playlist()
        if os.path.isfile(path):
            self.player.add_song(path)
        else:
            added = 0      
            for file_name in sorted(os.listdir(path)):
                file = path + "/" + file_name
                if os.path.isfile(file):
                    mime_type = mimetypes.guess_type(file)[0]
                    if mime_type is not None:
                        if (self.is_playlist(file) == False) and mime_type.startswith("audio"):
                            if mime_type:
                                self.player.add_song(file)
                                added += 1
            logger.info(f"Added {added} songs to playlist")

    def get_uuid_from_tag(self):
        if (self.tag is None) or (self.tag.is_present == False):
            raise NoTagException("Tag was not present when looking up UUID.")
        if (self.tag.ndef is None):
            logger.error(f"No message present on tag.")
            raise NoTagException("Tag had no NDEF object.")
        logger.debug(f"Getting tag UUID")
        l = message_decoder(self.tag.ndef.octets)
        if len(list(l)) > 0:
            uuid = list(message_decoder(self.tag.ndef.octets))[0].text
            logger.info(f"Tag UUID: {uuid}")
            return uuid
        else:
            return None
        
    def wait_for_tag_to_be_removed(self):
        while self.tag.is_present:
            time.sleep(0.5)

    def select_media_folder(self):
        folder = None
        base_path = None
        while folder is None:
            if self.default_directory is None:
                folder = filedialog.askdirectory(initialdir = base_path)
                if type(folder) == list:
                    folder = None
                    continue
                base_path = os.path.dirname(folder)
            else:
                folder = filedialog.askdirectory(initialdir = self.default_directory)
                if type(folder) == list:
                        folder = None
                        continue
        return folder
    
    def write_tag(self, default_directory):
        
        self.default_directory = default_directory
        
        while True:
            try:
                print(self.scan_ascii_msg)
                self.wait_for_tag()
                uuid_key = self.get_uuid_from_tag()
                path = None

                try:
                    path = self.DB.get_path(uuid_key)
                except NoPathException:
                    pass

                if (uuid_key is not None) and (path is not None):
                    logger.info(f"Tag is already pointing to: {path}")
                    ans = ""
                    while ans.upper() not in ["Y","N"]:
                        ans = input("Do you want to overwrite this tag Y/N: ")
                    if (ans.upper() == "N"):
                        print(f"Please remove tag.")
                        self.wait_for_tag_to_be_removed()
                        continue
                    else:
                        self.DB.remove_entry(uuid_key)

                # Get random UUID
                uuid_key = "NFCMP_" + str(uuid.uuid4())

                # Try to write tag.
                self.tag.ndef.records = [TextRecord(uuid_key)]
                
                # Get folder with music.
                logger.info("Opening file dialog...\nSelect album")
                folder = self.select_media_folder()
                
                # Try to create DB entry.
                self.DB.create_entry(uuid_key, folder)
                logger.info(f"Succesfully added entry to DB : {uuid_key},{folder}")

                # Done, wait for tag to be removed.
                logger.info(f"Done writing, please remove.")
                self.wait_for_tag_to_be_removed()

            except NoTagException as NTE:
                logger.error(f"{NTE}")
                continue
            except nfc.tag.TagCommandError as err:
                self.DB.remove_entry(uuid_key)
                logger.error(f"NDEF write failed: {str(err)}")
                logger.error(f"You probably removed the tag before its UUID could be written.")
            except KeyboardInterrupt:
                logger.info(f"Quitting...")
                self.player.stop()
                return
            except BadEntryException as err:
                logger.critical(f"Adding entry to DB failed: {str(err)}")

parser = argparse.ArgumentParser(
                    prog='NFC Media',
                    description='Physical interface for a digital library.')

parser.add_argument('-w', '--write',
                    action='store_true')

parser.add_argument('-d', '--default_directory')

def main():

    logger.info('Started')
    args = parser.parse_args()
    NFC_player = NFC_Player()

    if args.write:
        NFC_player.write_tag(args.default_directory)
    else:
        NFC_player.standby()
    logger.info("Done")

if __name__ == '__main__': 
    sys.exit(main())