import os
from os.path import isfile, join
import argparse
import psutil
import ffmpeg
import timeit
import time
from faster_whisper import WhisperModel as whisper
from faster_whisper.utils import available_models
from pathlib import Path
from typing import Iterator, TextIO
from download_best import Download
import validators
from download_best import Download
from get_audio import AudioProcess
import pdb

# Note:
# by default large model is used and float32 precision
#
#
video_supported = [".mkv", ".mov",  ".avi", ".mp4"]
audio_supported = [".mp3", ".wave", ".aac", ".flac"]
temp_audio_filepath = "temp_audio.aac"

def sizes_supported() -> list[str]:
	return available_models()

def srt_format_timestamp(seconds: float):
	assert seconds >= 0, "non-negative timestamp expected"
	milliseconds = round(seconds * 1000.0)

	hours = milliseconds // 3_600_000
	milliseconds -= hours * 3_600_000

	minutes = milliseconds // 60_000
	milliseconds -= minutes * 60_000

	seconds = milliseconds // 1_000
	milliseconds -= seconds * 1_000

	return (f"{hours}:") + f"{minutes:02d}:{seconds:02d},{milliseconds:03d}"

def write_srt(segments, file: TextIO):

	print("\nBegin transcription and creating subtitle file:")
	print("-------------------------------------------------------")

	count = 0
	for segment in segments:
		count +=1
		print(
			f"{count}\n"
			f"{srt_format_timestamp(segment.start)} --> {srt_format_timestamp(segment.end)}\n"
			f"{segment.text.replace('-->', '->').strip()}\n",
			file=file,
			flush=True,
		)

def isVideoFile(file_suffix):
	file_suffix = file_suffix.lower()
	if file_suffix in video_supported:
		return True

	return False

def isAudioFile(file_suffix):
	file_suffix = file_suffix.lower()
	if file_suffix in audio_supported:
		return True

	return False

def findarg(args, key: str) -> bool:
	return key in args and getattr(args, key)

def get_fullpath(output_dir, output_file) -> tuple[Path, str]:
	output_filepath = Path(output_file).resolve()

	if output_filepath.parent != Path(output_dir).resolve():
		output_dir = output_filepath.parent
		output_file = Path(output_file).name

	return Path(output_dir, output_file), output_dir

def transcribe(args, model, full_filepath, filename = None):

	assert type(full_filepath) == str

	if filename is None: # same as path implicitly
		filename = full_filepath
	#pdb.set_trace()

	start_time = timeit.default_timer()
	segments, stats = model.transcribe(full_filepath, beam_size=args.beam_size, language=args.language)

	print("\nDetected language '%s' with probability %f" % (stats.language, stats.language_probability))

	# save SRT
	srt_subtitle_filename = join(args.output_dir, Path(filename).stem + "." + time.strftime("%Y%m%d-%H%M%S") + f".{args.language}.srt")

	with open(srt_subtitle_filename, "w") as srt:
		write_srt(segments, file=srt)

	print(filename," took ","{:.1f}".format(timeit.default_timer() - start_time)," seconds")
	print("-------------------------------------------------------")

def initialize(args):

	print("--------------------INITIALIZING-----------------------")

	# cleanup the audio file that is no longer needed
	if isfile(temp_audio_filepath):
		os.remove(temp_audio_filepath)

	# initialize the model with given args
	if args.device != "cuda":
		#overrides the os.environ["OMP_NUM_THREADS"]
		threads = str(args.nproc)
		model = whisper(args.model_size, cpu_threads=threads, num_workers=4, device=args.device, compute_type=args.precision)
	else:
		model = whisper(args.model_size, device=args.device, compute_type=args.precision)

	return model;

def close(args):

	if not findarg(args, 'keep') and isfile(temp_audio_filepath):
		os.remove(temp_audio_filepath)

	if findarg(args, 'filename') and isfile(args.filename):
		args.filename.close()

def add_media_files(args, video_files, audio_files, debug = False, verbose = False):
	#pdb.set_trace()

	if findarg(args, 'filename'):
		if validators.url(args.filename):
			args.url         = args.filename
			args.audio_only  = True
			args.overwrite   = True
			args.verbose     = False

			download = Download(args, debug)
			audio_file_list, retcode = download.run()

			if retcode == 0:# and audio_file_list == args.output_name:
				audio_files += audio_file_list
			else:
				print(f"Could not process the URL, code {retcode} and audio file {audio_file_list} and args.output_name {args.output_name}")
				close(args)
				exit(-1)

		elif Path(args.filename).exists():
			args.filename = get_fullpath(os.getcwd(), args.filename)
			if args.filename.exists():
				if isVideoFile(args.filename.suffix) == True:
					video_files.append(str(args.filename))
				elif isAudioFile(args.filename.suffix) == True:
					audio_files.append(str(args.filename))

	elif findarg(args, 'input_dir'):
		if Path(args.input_dir).exists():
			for filename in os.listdir(args.input_dir):
				filename = Path(args.input_dir, filename)
				if filename.is_file() and isVideoFile(filename.suffix) == True:
					video_files.append(str(filename));
				elif filename.is_file() and isAudioFile(filename.suffix) == True:
					audio_files.append(str(filename))
		else:
			raise FileNotFoundError(f"--input_dir argument does not specify a valid dir: {args.input_dir}")

	# did not find any files
	if len(video_files) + len(audio_files) == 0:
		if args.filename:
			print("URL is not valid")
			close(args)
			exit(-1)
		else:
			print("There were no media to process")
			close(args)
			exit(0)

	print(f"Media files found {len(video_files) + len(audio_files)}")


def main():

	global temp_audio_filepath
	video_files = []
	audio_files = []

	parser = argparse.ArgumentParser("Generates subtitiles of the video file as an input")
	parser.add_argument("-f", "--filename", help="Name of the media file stored in the filesystem or URL \
						of a video/audio file that needs to subtitles. URL can also be a list of media")
	parser.add_argument("-i", "--input_dir", help="Input directory where video files are. --filename overrides this")
	parser.add_argument("-af", "--audio_filter", help="Audio or video filters to use before transcription \
						(for ffmpeg), no spaces, just comma-separated")
	parser.add_argument("-o", "--output_name", help="Output filename in case of issues with title", default=os.getcwd())
	parser.add_argument("-od", "--output_dir", help="Ouput directory", default=os.getcwd())
	parser.add_argument("-l", "--language", help="Language to be translated from", default='en', type=str)
	parser.add_argument("-b", "--beam_size", help="Beam size parameter or best_of equivalent from Open-AI whisper", type=int, default=5)
	parser.add_argument("-p", "--precision", help="Precision to use to create the model", type=str, default="auto")
	parser.add_argument("-d", "--device", help="Device to use such a CPU or GPU", choices=["cpu", "cuda"], default="cuda")
	parser.add_argument("-s", "--model_size", help="Size of the model, default is small.", choices=sizes_supported(), default="small")
	parser.add_argument("-n", "--nproc", help="Number of CPUs to use", default=psutil.cpu_count(logical=False), type=int)
	parser.add_argument('-k', "--keep", help="Keep intermediate files", action='store_true')
	parser.add_argument("--verbose", help="Verbose print from dependent processes", action='store_true')
	parser.add_argument("--quiet", help="Debug print off", action='store_true')
	parser.add_argument("--playlist_start", help="Starting position from a list of media, to start downloading from")
	parser.add_argument("--playlist_end", help="Ending position from a list of media, to stop downloading at")

	args = parser.parse_args()

	# make the directory if missing, output_name override output_dir if former has dir already
	if findarg(args, 'output_name'):
		args.output_name, args.output_dir = get_fullpath(args.output_dir, args.output_name)
	Path(args.output_dir).mkdir(parents=True, exist_ok=True)

	# print out the settings, some of them belongs to dependent scripts such as downloader or get-audio
	if args.quiet == False:
		print("-----------------------SETTINGS------------------------")
		arguments = vars(args)
		for arg in arguments:
			print(arg, '\t', getattr(args, arg))

	#if args.output_name:
	#	temp_audio_filename = Path(args.output_dir, args.output_name)
	#else:
	temp_audio_filepath = str(Path(args.output_dir, temp_audio_filepath))

	add_media_files(args, video_files, audio_files, debug = not args.quiet, verbose = args.verbose)
	model = initialize(args);

	# convert the videofile into audiofile before processing
	for videofile in video_files:
		if args.quiet == False:
			print(f"Processing file {videofile} and using audio filter")

		audio_processor = AudioProcess(args, debug=not args.quiet)
		audio_processor.extract_audio(input_filepath=videofile, output_filepath=temp_audio_filepath, overwrite=True);

		# output_name is set by setting output_filepath
		transcribe(args, model, temp_audio_filepath, videofile)

	# convert the audiofile before processing
	for audiofile in audio_files:
		if args.quiet == False:
			print(f"Processing file: {audiofile}")

		if args.audio_filter:
			if args.quiet == False:
				print(f"Using audio filter on: {audiofile} as: {args.audio_filter}")

			audio_processor = AudioProcess(args, debug=not args.quiet)
			audio_processor.extract_audio(input_filepath=audiofile, output_filepath=temp_audio_filepath, overwrite=True);

			# output_name is set by setting output_filepath
			transcribe(args, model, temp_audio_filepath, audiofile)

		else:
			transcribe(args, model, audiofile)

	# cleanup the audio file that is no longer needed
	close(args)
	print("Done.")

	return 0

if __name__ == "__main__":
	main()
