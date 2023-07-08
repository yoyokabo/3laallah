"""
MusAI

Author: Nicolás Iglesias
Email: nfiglesias@gmail.com

This file is part of MusAI, a project for generating MIDI music using 
a combination of machine learning algorithms.

Below is a monolitic script that constructs a corpora of MIDI tokens ready to be injected 
into the model. Semantical processing, cleanup and sanitization happens here. You can 
optionally choose to process MIDIs which use a set of instrument classes and/or extract 
a specific set of classes from the songs.
It uses Ray for parallel processing.

MIT License
Copyright (c) [2023] [Nicolás Iglesias]

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import glob
import json
import os
import re
import argparse
import ray
import psutil
import gc
from asyncio import Event
from ray.actor import ActorHandle
from itertools import chain
from loguru import logger
from typing import Tuple
from pathlib import Path
from miditok import REMIPlus, MMM
from miditok.constants import ADDITIONAL_TOKENS, BEAT_RES, INSTRUMENT_CLASSES
from miditok.utils import merge_tracks_per_class, merge_same_program_tracks, get_midi_programs
from miditoolkit import MidiFile
from tqdm import tqdm
from functools import reduce
from operator import iconcat

# initialize variables
os.environ['FUNCTION_SIZE_ERROR_THRESHOLD'] = '512'
print("check")
TOKENS_PATH = './tok/'
MIDIS_PATH = './raw/'
TOKEN_PARAMS_NAME = 'token_params.cfg'
TOKEN_PARAMS_PATH = Path(f'{TOKENS_PATH}/{TOKEN_PARAMS_NAME}')

PITCH_RANGE = range(21, 109)
CLASSES_PERCUSSION = [1, 14, 16]
CLASSES_SYNTHS = [10, 11]
CLASSES_STRINGS = [5, 6]
CLASSES_GUITAR_BASS = [3, 4]
CLASS_REED = [8, 9]
CLASS_EFFECTS = [12, 15]
BINS_VELOCITY = (24)
BINS_TEMPO = (24)

TOKENIZER_ALGOS = ['REMI', 'MMM']

# declare Ray related stuff


@ray.remote
class ProgressBarActor:
    counter: int
    delta: int
    event: Event

    def __init__(self) -> None:
        self.counter = 0
        self.delta = 0
        self.event = Event()

    def update(self, num_items_completed: int) -> None:
        """Updates the ProgressBar with the incremental
        number of items that were just completed.
        """
        self.counter += num_items_completed
        self.delta += num_items_completed
        self.event.set()

    async def wait_for_update(self) -> Tuple[int, int]:
        """Blocking call.

        Waits until somebody calls `update`, then returns a tuple of
        the number of updates since the last call to
        `wait_for_update`, and the total number of completed items.
        """
        await self.event.wait()
        self.event.clear()
        saved_delta = self.delta
        self.delta = 0
        return saved_delta, self.counter

    def get_counter(self) -> int:
        """
        Returns the total number of complete items.
        """
        return self.counter
# Back on the local node, once you launch your remote Ray tasks, call
# `print_until_done`, which will feed everything back into a `tqdm` counter.


class ProgressBar:
    progress_actor: ActorHandle
    total: int
    description: str
    pbar: tqdm

    def __init__(self, total: int, description: str = ""):
        # Ray actors don't seem to play nice with mypy, generating
        # a spurious warning for the following line,
        # which we need to suppress. The code is fine.
        self.progress_actor = ProgressBarActor.remote()  # type: ignore
        self.total = total
        self.description = description

    @property
    def actor(self) -> ActorHandle:
        """Returns a reference to the remote `ProgressBarActor`.

        When you complete tasks, call `update` on the actor.
        """
        return self.progress_actor

    def print_until_done(self) -> None:
        """Blocking call.

        Do this after starting a series of remote Ray tasks, to which you've
        passed the actor handle. Each of them calls `update` on the actor.
        When the progress meter reaches 100%, this method returns.
        """
        pbar = tqdm(desc=self.description, total=self.total)
        while True:
            delta, counter = ray.get(self.actor.wait_for_update.remote())
            pbar.update(delta)
            if counter >= self.total:
                pbar.close()
                return


# initialize logger
logger.add('tokenizer_errors_{time}.log', delay=True,
           backtrace=True, diagnose=True, level='ERROR', rotation='10 MB')

# begin program


def deco(func): return func


if __name__ == "__main__":
    # parse command line arguments
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument('-t', '--tokens_path', default=TOKENS_PATH,
                            help='The output path were tokens are saved', type=str)
    arg_parser.add_argument('-m', '--midis_path', default=MIDIS_PATH,
                            help='The path where MIDI files can be located', type=str)
    arg_parser.add_argument('-g', '--midis_glob', default='*.mid',
                            help='The glob pattern used to locate MIDI files', type=str)
    arg_parser.add_argument('-b', '--bpe', help='Applies BPE to the corpora of tokens',
                            action='store_true', default=False)
    arg_parser.add_argument('-p', '--process', help='Extracts tokens from the MIDI files',
                            action='store_true', default=False)
    arg_parser.add_argument('-a', '--algo', help='Tokenization algorithm',
                            choices=TOKENIZER_ALGOS, default='MMM', type=str)
    arg_parser.add_argument('-c', '--classes', help='Only extract this instruments classes (e.g. 1,14,16,3,4,10,11)',
                            default=None, type=str)
    arg_parser.add_argument('-r', '--classes_req', help='Minimum set of instruments classes required (e.g. 1,14,16)',
                            default=None, type=str)
    arg_parser.add_argument('-l', '--length', help='Minimum sequence length (in beats)',
                            default=16, type=int)
    arg_parser.add_argument(
        '-d', '--debug', help='Debug mode.', action='store_true', default=False)
    args = arg_parser.parse_args()

    # callback handlers via Ray or not
    deco = ray.remote(num_cpus=0.5) if not args.debug else deco

# define some functions


def auto_garbage_collect(pct=80.0):
    """
    auto_garbage_collection - Call the garbage collection if memory used is greater than 80% of total available memory.
                              This is called to deal with an issue in Ray not freeing up used memory.

        pct - Default value of 80%.  Amount of memory in use that triggers the garbage collection call.
    """
    if psutil.virtual_memory().percent >= pct:
        gc.collect()

    return


def to_iterator(obj_ids, debug=False):
    if debug:
        return obj_ids

    while obj_ids:
        done, obj_ids = ray.wait(obj_ids)
        yield ray.get(done[0])


def filter_programs(skip_programs):
    all_programs = range(-1, 128)
    keep_programs = list(set(all_programs) - set(skip_programs))

    return keep_programs


def get_other_programs(classes):
    programs = []

    for i in range(0, len(INSTRUMENT_CLASSES)):
        if i not in classes:
            programs += list(
                INSTRUMENT_CLASSES[i]['program_range'])

    return programs


def parse_bpe_tokens(tokenizer, tokens):
    seq = [next(key for key, value in tokenizer.vocab_bpe.items() if value == tid)
           for tid in tokens]
    tseq = [tokenizer._vocab_bpe_bytes_to_tokens[bpetoken] for bpetoken in seq]

    return list(chain.from_iterable(tseq))


def get_tokenizer(params=None, algo='MMM', programs=None):
    """Returns a tokenizer.

    :param params: Path to a token_params.cfg file for preloading, defaults to None
    :type params: str, optional
    :return: A MMM or REMIPlus tokenizer.
    :rtype: MIDITokenizer
    """
    if algo not in TOKENIZER_ALGOS:
        raise 'Invalid tokenization algorithm'

    additional_tokens = ADDITIONAL_TOKENS
    additional_tokens['Chord'] = True
    additional_tokens['TimeSignature'] = True
    additional_tokens['Program'] = True
    additional_tokens['nb_tempos'] = BINS_TEMPO

    if programs != None:
        additional_tokens['programs'] = programs

    tokenizer = None

    if algo == 'REMI':
        tokenizer = REMIPlus(pitch_range=PITCH_RANGE,
                             additional_tokens=additional_tokens,
                             nb_velocities=BINS_VELOCITY,
                             params=params)
    elif algo == 'MMM':
        tokenizer = MMM(pitch_range=PITCH_RANGE,
                        additional_tokens=additional_tokens,
                        nb_velocities=BINS_VELOCITY,
                        params=params)

    logger.info('Tokenizer initialized. Using {algo}', algo=algo)

    return tokenizer


@deco
def process_midi(midi_path, pba: ActorHandle, classes=None, classes_req=None, minlength=16, debug=False):
    midi_doc = None
    midi = None

    try:
        midi = MidiFile(midi_path)
    except Exception as err:
        pass

    if midi != None \
            and not (
                (midi.max_tick/midi.ticks_per_beat) < minlength
                and
                midi.ticks_per_beat < max(BEAT_RES.values()) * 4
            ):
        programs = get_midi_programs(midi)
        midi_programs = list(set([p[0] for p in programs]))
        drum_programs = list(set([p[0] for p in programs if p[1] == True]))

        # check if midi has at least one program from each required class
        meets_req = True

        if classes_req != None:
            classes_req = classes_req.strip().split(',')
            classes_req = [int(c.strip()) for c in classes_req]

            for ic in classes_req:
                class_programs = list(INSTRUMENT_CLASSES[ic]['program_range'])
                intersect = list(set(midi_programs) & set(class_programs))
                meets_req = (ic == 16 and len(drum_programs) > 0) \
                    or meets_req and len(intersect) > 0

                if not meets_req:
                    break

        if meets_req:
            # remove unwanted tracks
            programs_to_delete = []

            if classes is None:
                for ic in CLASS_EFFECTS:
                    programs_to_delete += list(
                        INSTRUMENT_CLASSES[ic]['program_range'])
            else:
                classes = classes.strip().split(',')
                classes = [int(c.strip()) for c in classes]
                programs_to_delete = get_other_programs(classes)

            keep_programs = filter_programs(programs_to_delete)

            # some drum tracks use non-standard programs
            if classes != None and 16 in classes \
                    and len(drum_programs) > 0:
                keep_programs = list(set(keep_programs + drum_programs))

            # remove unwanted tracks
            merge_tracks_per_class(midi, valid_programs=keep_programs)

            # discard empty songs
            if len(midi.instruments) >= 1:
                if classes is None:
                    # merge percussion/drums
                    merge_tracks_per_class(midi, CLASSES_PERCUSSION)

                    # merge synths
                    merge_tracks_per_class(midi, CLASSES_SYNTHS)

                    # merge strings
                    merge_tracks_per_class(midi, CLASSES_STRINGS)

                    # merge guitar & bass
                    merge_tracks_per_class(midi, CLASSES_GUITAR_BASS)

                # merge_same_program_tracks(midi.instruments)

                midi_name = re.sub(r'[^0-9a-z_]{1,}', '_',
                                   str.lower(os.path.basename(midi_path)))

                programs = list(set([program[0]
                                for program in get_midi_programs(midi)]))

                midi_doc = {
                    'programs': programs,
                    'path': midi_path,
                    'name': midi_name
                }

    if pba != None:
        pba.update.remote(1)

    return midi_doc


@deco
def tokenize_set(midi_doc, tokens_path, tokenizer, pba: ActorHandle, bpe=False, debug=False):
    midi = None
    tokens_cfg = None

    try:
        midi = MidiFile(midi_doc['path'])
    except:
        pass

    if midi != None:
        tokens_cfg = f"{tokens_path}/{midi_doc['name']}.json"
        programs = midi_doc['programs']

        try:
            # remove unwanted tracks
            merge_tracks_per_class(midi, valid_programs=programs)

            tokens = tokenizer.midi_to_tokens(midi, apply_bpe_if_possible=bpe)
            tokenizer.save_tokens(tokens, tokens_cfg, programs=programs)
        except Exception as error:
            tokens_cfg = None
        finally:
            del midi

    if not debug:
        pba.update.remote(1)

    return tokens_cfg


def get_collection_refs(midis_path=None, midis_glob=None, classes=None, classes_req=None, minlength=16, debug=False):
    """Pre-process and retrieves a collection of MIDI files, ready for tokenization.

    :return: A dictionary containing a set of {'programs': ..., 'path': ..., 'name': ...} 
            for each MIDI file in the collection.
    :rtype: dict
    """
    midi_file_paths = list(Path(midis_path).glob(midis_glob))
    print(midi_file_paths)
    logger.info(
        'Processing collection: {coll_size} MIDI files', coll_size=len(midi_file_paths))

    # process MIDIs via Ray
    if not debug:
        pb = ProgressBar(len(midi_file_paths))
        actor = pb.actor
    else:
        actor = None
        midi_file_paths = tqdm(midi_file_paths)

    process_call = process_midi.remote if not debug else process_midi
    ray_refs = [process_call(midi_path, actor, classes=classes, classes_req=classes_req, minlength=minlength, debug=debug)
                for midi_path in midi_file_paths]

    if not debug:
        pb.print_until_done()

    return [ref for ref in ray_refs if ref != None]


# begin program
if __name__ == "__main__":
    MIDI_TITLES = []
    MIDI_PROGRAMS = []
    print("check")

    if args.process:
        if not args.debug:
            # starts orchestration
            ray.init(num_cpus=psutil.cpu_count())

            MIDI_COLLECTION_REFS = get_collection_refs(
                args.midis_path, args.midis_glob, args.classes, args.classes_req, args.length)

            for ray_midi_ref in MIDI_COLLECTION_REFS:
                midi_doc = ray.get(ray_midi_ref)

                if midi_doc != None:
                    MIDI_TITLES.append(midi_doc['name'])
                    MIDI_PROGRAMS.append(midi_doc['programs'])
        else:
            MIDI_COLLECTION_REFS = get_collection_refs(
                args.midis_path, args.midis_glob, args.classes, args.classes_req, args.length, args.debug)
            MIDI_TITLES = [midi_ref['name'] for midi_ref in MIDI_COLLECTION_REFS]
            MIDI_PROGRAMS = [midi_ref['programs'] for midi_ref in MIDI_COLLECTION_REFS]

        logger.info('Processing tokenization: {collection_size} documents', collection_size=len(
            MIDI_COLLECTION_REFS))

        Path(args.tokens_path).mkdir(parents=True, exist_ok=True)

        # collect used programs
        programs_used = [program for program in list(
            set(reduce(iconcat, MIDI_PROGRAMS, [])))]

        # initializes tokenizer
        TOKENIZER = get_tokenizer(programs=programs_used)

        # process tokenization via Ray
        if not args.debug:
            pb = ProgressBar(len(MIDI_COLLECTION_REFS))
            actor = pb.actor
            midi_collection_refs = MIDI_COLLECTION_REFS
        else:
            actor = None
            midi_collection_refs = tqdm(MIDI_COLLECTION_REFS)

        tokenize_call = tokenize_set if args.debug else tokenize_set.remote
        ray_tokenized_refs = [tokenize_call(ray_midi_ref, args.tokens_path, TOKENIZER,
                                            actor, bpe=args.bpe, debug=args.debug) for ray_midi_ref in midi_collection_refs]

        if not args.debug:
            pb.print_until_done()
            ray.shutdown()

        logger.info('Vocab size (base): {vocab_size}',
                    vocab_size=len(TOKENIZER.vocab))
        logger.info('Saving params...')

        """ !IMPORTANT always store the _vocab_base when saving params. 
        Order of keys in the vocab may differ in a new instance of a preloaded TOKENIZER. """
        TOKENIZER.save_params(
            f'{args.tokens_path}/{TOKEN_PARAMS_NAME}', {'_vocab_base': TOKENIZER.vocab})

    if args.bpe:
        # Constructs the vocabulary with BPE, from the tokenized files
        token_files_paths = list(Path(args.tokens_path).glob('*.json'))
        tokens_bpe_path = f'{args.tokens_path}/bpe'

        Path(tokens_bpe_path).mkdir(parents=True, exist_ok=True)

        if not args.process:
            TOKENIZER = get_tokenizer(params=f'{args.tokens_path}/{TOKEN_PARAMS_NAME}')

        logger.info('Learning BPE from vocab size {vocab_size}...', vocab_size=len(
            TOKENIZER))

        TOKENIZER.learn_bpe(
            vocab_size=int(len(TOKENIZER.vocab)*1.25),
            tokens_paths=[path for path in token_files_paths if path != None],
            start_from_empty_voc=False,
        )

        logger.info('Applying BPE...')
        TOKENIZER.apply_bpe_to_dataset(args.tokens_path, tokens_bpe_path)

        logger.info('Saving params with BPE applied...')
        TOKENIZER.save_params(f'{tokens_bpe_path}/{TOKEN_PARAMS_NAME}')

        logger.info('Vocab size (BPE): {vocab_size}',
                    vocab_size=len(TOKENIZER))

    ray.shutdown()
