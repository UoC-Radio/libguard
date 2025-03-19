#include <Python.h>
#include <structmember.h>
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/opt.h>
#include <libavutil/channel_layout.h>
#include <libswresample/swresample.h>
#include <ebur128.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <math.h>

#define LOG_ERROR(msg) fprintf(stderr, "Error: %s\n", msg)


/* ReplayGain 2 reference level in LUFS (EBU R128) */
#define RG2_REFERENCE -18.0f

enum aunlz_errors {
	ERR_OK,
	ERR_NOMEM,
	ERR_NOFILE,
	ERR_NOSTREAM,
	ERR_NOCODEC,
	ERR_FMT,
	ERR_CODEC_INIT,
	ERR_CODEC,
	ERR_EBUR128_INIT,
	ERR_EBUR128,
	ERR_SWR_INIT,
	ERR_SWR
};

typedef struct {
	PyObject_HEAD
	const char *format_name;
	uint32_t sample_rate;
	uint32_t bit_rate;
	uint8_t bit_depth;
	uint32_t duration_secs;
	uint32_t duration_diff;
	uint64_t total_frames;
	double ebur128_iloud;		/* Integrated loudness */
	double relative_threshold;	/* Gating threshold used */
	float ebur128_lra;		/* Loudness range */
	float sample_peak;
	float rg2_gain;
} AunlzResults;

struct aunlz_state {
	AVFormatContext *fmt_ctx;
	AVCodecContext *codec_ctx;
	SwrContext *swr_ctx;
	AVFrame *converted_avframe;
	ebur128_state *ebur128_ctx;
	int audio_stream_idx;
	AunlzResults track_info;
	int ffmpeg_err;
	int libebur128_err;
	int err;
};

/*********\
* HELPERS *
\*********/

/* Function to get a descriptive error message for ffmpeg error codes */
static const char* aunlz_get_ffmpeg_error_message(int error_code) {
	static char error_buffer[128];
	
	/* av_strerror returns a descriptive error message into a user-provided buffer */
	if (av_strerror(error_code, error_buffer, sizeof(error_buffer)) < 0) {
		snprintf(error_buffer, sizeof(error_buffer), "Unknown FFmpeg error code: %d", error_code);
	}
	
	return error_buffer;
}

/* Function to get a descriptive error message for ebur128 error codes */
static const char* aunlz_get_ebur128_error_message(int error_code) {
	switch (error_code) {
		case EBUR128_SUCCESS:
			 "Success";
		case EBUR128_ERROR_NOMEM:
			return "Not enough memory";
		case EBUR128_ERROR_INVALID_MODE:
			return "Invalid mode";
		case EBUR128_ERROR_INVALID_CHANNEL_INDEX:
			return "Invalid channel index";
		case EBUR128_ERROR_NO_CHANGE:
			return "No change";
		default:
			static char buffer[64];
			snprintf(buffer, sizeof(buffer), "Unknown libebur128 error: %d", error_code);
			return buffer;
		}
}

/* Function to get a descriptive error message for analyzer error codes */
static const char* aunlz_get_analyzer_error_message(int error_code) {
	switch (error_code) {
		case ERR_OK:
			return "Success";
		case ERR_NOMEM:
			return "Memory allocation failed";
		case ERR_NOFILE:
			return "File not found or not accessible";
		case ERR_NOSTREAM:
			return "No audio stream found in file";
		case ERR_NOCODEC:
			return "No suitable codec found for audio stream";
		case ERR_FMT:
			return "Format error (invalid stream info)";
		case ERR_CODEC_INIT:
			return "Failed to initialize audio codec";
		case ERR_CODEC:
			return "Error while decoding audio stream";
		case ERR_EBUR128_INIT:
			return "Failed to initialize EBU R128 loudness analyzer";
		case ERR_EBUR128:
			return "Error while performing EBU R128 loudness analysis";
		case ERR_SWR_INIT:
			return "Failed to initialize audio resampler";
		case ERR_SWR:
			return "Error during audio resampling";
		default:
			static char buffer[64];
			snprintf(buffer, sizeof(buffer), "Unknown error: %d", error_code);
			return buffer;
	}
}

static const char* aunlz_get_combined_error_msg(struct aunlz_state *st)
{
	static char full_error_message[256];
	const char *analyzer_msg = aunlz_get_analyzer_error_message(st->err);

	if (st->ffmpeg_err) {
		const char *ffmpeg_msg = aunlz_get_ffmpeg_error_message(st->ffmpeg_err);
		snprintf(full_error_message, sizeof(full_error_message), 
			"Audio analyzer error: %s. FFmpeg error: %s", 
			analyzer_msg, ffmpeg_msg);
	} else if (st->libebur128_err) {
		const char *ebur128_msg = aunlz_get_ebur128_error_message(st->libebur128_err);
		snprintf(full_error_message, sizeof(full_error_message), 
			"Audio analyzer error: %s. libebur128 error: %s", 
			analyzer_msg, ebur128_msg);
	} else {
		snprintf(full_error_message, sizeof(full_error_message), 
		"Audio analyzer error: %s", analyzer_msg);
	}
	return full_error_message;
}

static int aunlz_get_bit_depth(struct aunlz_state *st)
{
	AVFormatContext *fmt_ctx = st->fmt_ctx;
	AVCodecContext *codec_ctx = st->codec_ctx;
	AVCodecParameters *codecpar = st->fmt_ctx->streams[st->audio_stream_idx]->codecpar;

	/* First check bits_per_coded_sample (what went in)
	 * this is the most direct indicator */
	if (codecpar->bits_per_coded_sample > 0)
		return codecpar->bits_per_coded_sample;
	if (codec_ctx->bits_per_coded_sample > 0)
		return codec_ctx->bits_per_coded_sample;

	/* Next check bits_per_raw_sample (what comes out) */
	if (codecpar->bits_per_raw_sample > 0)
		return codecpar->bits_per_raw_sample;
	if (codec_ctx->bits_per_raw_sample > 0)
		return codec_ctx->bits_per_raw_sample;
	
	/* Check based on codec ID and format-specific information */

	/* For PCM formats, directly use the sample format */
	if (codecpar->codec_id >= AV_CODEC_ID_PCM_S16LE && 
	    codecpar->codec_id <= AV_CODEC_ID_PCM_F64BE) {
		/* Use av_get_bytes_per_sample to determine bit depth */
		int bytes_per_sample = av_get_bytes_per_sample(codec_ctx->sample_fmt);
		if (bytes_per_sample > 0)
			return bytes_per_sample * 8;
	}

	switch (codecpar->codec_id) {
		case AV_CODEC_ID_FLAC:
			/* FLAC can be 16, 20, 24, or 32 bits */
			/* Try to get from codec private data if available */
			if (codecpar->extradata && codecpar->extradata_size >= 8) {
				int streaminfo_bits = (codecpar->extradata[7] >> 4) & 0x0F;
				if (streaminfo_bits > 0) {
					return streaminfo_bits + 1; /* FLAC stores as bits-1 */
				}
			}
			/* Most common is 16-bit */
			return 16;
		case AV_CODEC_ID_MP3:
			/* MP3 is always 16-bit */
			return 16;
		case AV_CODEC_ID_VORBIS:
		case AV_CODEC_ID_OPUS:
			/* These are lossy codecs, but typically encode at equivalent of 16-bit */
			return 16;
		case AV_CODEC_ID_AAC:
			/* AAC typically encodes at equivalent of 16-bit */
			return 16;
		default:
			/* Use av_get_bytes_per_sample as final fallback */
			if (codec_ctx->sample_fmt != AV_SAMPLE_FMT_NONE) {
				int bytes_per_sample = av_get_bytes_per_sample(codec_ctx->sample_fmt);
				if (bytes_per_sample > 0)
					return bytes_per_sample * 8;
			}
			/* Absolute last resort */
			return 16;
	}
}

static int aunlz_get_bit_rate(struct aunlz_state *st)
{
	AVFormatContext *fmt_ctx = st->fmt_ctx;
	AVCodecContext *codec_ctx = st->codec_ctx;
	AVCodecParameters *codecpar = st->fmt_ctx->streams[st->audio_stream_idx]->codecpar;
	int audio_stream_count = 0;

	/* CodecParameters usually have the most accurate stream-specific bitrate */
	if (codecpar->bit_rate)
		return codecpar->bit_rate;

	/* Format context has a calculated average bitrate
	 * (among streams), useful for lossless. */
	for (int i = 0; i < (int)fmt_ctx->nb_streams; i++) {
		if (fmt_ctx->streams[i]->codecpar->codec_type == AVMEDIA_TYPE_AUDIO)
			audio_stream_count++;
	}
	if (!audio_stream_count)
		return -1;
	if (fmt_ctx->bit_rate > 0) {
		/* Assume even distribution.*/
		return fmt_ctx->bit_rate / audio_stream_count;
	}

	/* Codec context might have bitrate info from the decoder */
	if (codec_ctx->bit_rate)
		return codec_ctx->bit_rate;

	/* Try to estimate it based on the file size / duration */
	if (st->track_info.duration_secs <= 0)
		return -1;

	ssize_t file_size = 0;
	file_size = avio_size(fmt_ctx->pb);
	if (file_size <= 0)
		return -1;

	size_t audio_size = file_size / audio_stream_count;

	return (int)((audio_size * 8) / st->track_info.duration_secs);
}


/**************\
* CLEANUP/INIT *
\**************/

static void aunlz_cleanup(struct aunlz_state *st)
{
	if (st->fmt_ctx)
		avformat_close_input(&st->fmt_ctx);
	if (st->codec_ctx)
		avcodec_free_context(&st->codec_ctx);
	if (st->ebur128_ctx)
		ebur128_destroy(&st->ebur128_ctx);
	if (st->swr_ctx)
		swr_free(&st->swr_ctx);
	if (st->converted_avframe)
		av_frame_free(&st->converted_avframe);
}

static int aunlz_init(struct aunlz_state *st, const char* filepath, int do_ebur128, int do_lra)
{
	AunlzResults *track_info = &st->track_info;
	int ret = 0;

	/* Prevent ffmpeg from spamming us (e.g each time we load an mp3
	 * and get that warning on inaccurate duration)*/
	av_log_set_level(AV_LOG_PANIC);

	/* Open input file */
	ret = avformat_open_input(&st->fmt_ctx, filepath, NULL, NULL);
	if (ret < 0) {
		st->ffmpeg_err = ret;
		return ERR_NOFILE;
	}

	/* Find stream info */
	ret = avformat_find_stream_info(st->fmt_ctx, NULL);
	if (ret < 0) {
		st->ffmpeg_err = ret;
		ret = ERR_NOSTREAM;
		goto cleanup;
	}

	/* Find audio stream */
	st->audio_stream_idx = av_find_best_stream(st->fmt_ctx, AVMEDIA_TYPE_AUDIO, -1, -1, NULL, 0);
	if (st->audio_stream_idx < 0) {
		st->ffmpeg_err = ret;
		ret = ERR_NOSTREAM;
		goto cleanup;
	}

	/* Get decoder */
	AVCodecParameters *codecpar = st->fmt_ctx->streams[st->audio_stream_idx]->codecpar;
	const AVCodec *codec = avcodec_find_decoder(codecpar->codec_id);
	if (!codec) {
		st->ffmpeg_err = ret;
		ret = ERR_NOCODEC;
		goto cleanup;
	}

	/* Allocate codec context */
	st->codec_ctx = avcodec_alloc_context3(codec);
	if (!st->codec_ctx) {
		st->ffmpeg_err = ret;
		ret = ERR_NOCODEC;
		goto cleanup;
	}

	/* Copy codec parameters */
	ret = avcodec_parameters_to_context(st->codec_ctx, codecpar);
	if (ret < 0) {
		st->ffmpeg_err = ret;
		ret = ERR_CODEC_INIT;
		goto cleanup;
	}

	/* Request decoder output format to interleaved float (what ebur128 expects) */
	ret = av_opt_set_int(st->codec_ctx, "request_sample_fmt", AV_SAMPLE_FMT_FLT, 0);
	if (ret < 0) {
		st->ffmpeg_err = ret;
		ret = ERR_CODEC_INIT;
		goto cleanup;
	}

	/* Open the codec */
	ret = avcodec_open2(st->codec_ctx, NULL, NULL);
	if (ret < 0) {
		st->ffmpeg_err = ret;
		ret = ERR_CODEC_INIT;
		goto cleanup;
	}

	if (!do_ebur128)
		return ERR_OK;

	/* Initialize libebur128 with integrated loudness, sample peak and optionally loudness range modes */
	int ebur128_flags = EBUR128_MODE_I |
			    EBUR128_MODE_SAMPLE_PEAK |
			    (do_lra ? EBUR128_MODE_LRA : 0);
	st->ebur128_ctx = ebur128_init(st->codec_ctx->ch_layout.nb_channels,
				       st->codec_ctx->sample_rate, ebur128_flags);
	if (!st->ebur128_ctx) {
		st->libebur128_err = ret;
		ret = ERR_EBUR128_INIT;
		goto cleanup;
	}

	/* Check if the decoder accepted our format request,
	 * if not initialize swr so that we convert it after decoding */
	 if (st->codec_ctx->sample_fmt == AV_SAMPLE_FMT_FLT)
	 	return ERR_OK;

	/* Pre-alloc the output frame, try to determine its size from
	 * the decoder in case of codecs with fixed-size frames, and
	 * fallback to a safe default like 1 sec (same as sample rate) */
	st->converted_avframe = av_frame_alloc();
	if (st->codec_ctx->frame_size)
		st->converted_avframe->nb_samples = st->codec_ctx->frame_size;
	else
		st->converted_avframe->nb_samples = st->codec_ctx->sample_rate;


	av_channel_layout_copy(&st->converted_avframe->ch_layout, &st->codec_ctx->ch_layout);
	st->converted_avframe->format = AV_SAMPLE_FMT_FLT;
	st->converted_avframe->sample_rate = st->codec_ctx->sample_rate;

	ret = av_frame_get_buffer(st->converted_avframe, 0);
	if (ret < 0) {
		st->ffmpeg_err = ret;
		ret = ERR_NOMEM;
		goto cleanup;
	}

	/* Initialize resampler / converter */
	ret = swr_alloc_set_opts2(&st->swr_ctx,
		&st->converted_avframe->ch_layout, st->converted_avframe->format, st->converted_avframe->sample_rate,
		&st->codec_ctx->ch_layout, st->codec_ctx->sample_fmt, st->codec_ctx->sample_rate,
		0, NULL);
	if (ret < 0) {
		st->ffmpeg_err = ret;
		ret = ERR_SWR_INIT;
		goto cleanup;
	}

	ret = swr_init(st->swr_ctx);
	if (ret < 0) {
		st->ffmpeg_err = ret;
		ret = ERR_SWR_INIT;
		goto cleanup;
	}

	return ERR_OK;

 cleanup:
	aunlz_cleanup(st);
	return ret;
}


/*******************************\ 
* AUDIO FILE PARSING/PROCESSING *
\*******************************/

static int aunlz_fill_basic_info(struct aunlz_state *st)
{
	AVFormatContext *fmt_ctx = st->fmt_ctx;
	AVCodecContext *codec_ctx = st->codec_ctx;
	AunlzResults *track_info = &st->track_info;
	AVCodecParameters *codecpar = st->fmt_ctx->streams[st->audio_stream_idx]->codecpar;

	/* Format name / sample rate, if any of those is missing
	 * something very wrong happened. */
	if (!fmt_ctx->iformat || !fmt_ctx->iformat->name)
		return ERR_FMT;
	if (codecpar->sample_rate <= 0 && codec_ctx->sample_rate <= 0)
		return ERR_FMT;

	track_info->format_name = fmt_ctx->iformat->name;
	track_info->sample_rate = codecpar->sample_rate > 0 ? codecpar->sample_rate : codec_ctx->sample_rate;

	/* See if duration is available */
	if (fmt_ctx->duration != AV_NOPTS_VALUE) {
		track_info->duration_secs = (int)(((float)fmt_ctx->duration + 0.5f) / (float)AV_TIME_BASE);
	} else if (fmt_ctx->streams[st->audio_stream_idx]->duration != AV_NOPTS_VALUE) {
		AVRational tb = fmt_ctx->streams[st->audio_stream_idx]->time_base;
		track_info->duration_secs = (int)(fmt_ctx->streams[st->audio_stream_idx]->duration * av_q2d(tb));
	} else
		/* If this is a container format it should have duration in its metadata,
		 * if it's a streaming format ffmpeg should be able to get an estimate,
		 * if we end up here, it means that the file is either non-compliant, or
		 * ffmpeg failed to get a duration estimate. In any case this is not a
		 * normal scenario and the file should go for further inspection. We coud
		 * force a decode to calculate the duration but leave that to the caller.*/
		return ERR_FMT;


	track_info->bit_depth = aunlz_get_bit_depth(st);

	int ret = aunlz_get_bit_rate(st);
	if (ret < 0)
		return ERR_FMT;
	track_info->bit_rate = ret;

	return ERR_OK;
}

static int aunlz_process(struct aunlz_state *st, int do_lra)
{
	AVPacket *stream_packet = av_packet_alloc();
	AVFrame *decoded_avframe = av_frame_alloc();
	AVCodecContext *codec_ctx = st->codec_ctx;
	AunlzResults *track_info = &st->track_info;
	AVFrame *processing_frame = NULL;
	size_t total_samples = 0;
	int ret = 0;
	int eof_reached = 0;

	if (!stream_packet || !decoded_avframe) {
		ret = ERR_NOMEM;
		goto cleanup;
	}

	while (!eof_reached) {
		/* Try to get next avframe from the decoder, note that according
		 * to docs, this will also unref decoded_frame before providing a new one.*/
		ret = avcodec_receive_frame(codec_ctx, decoded_avframe);
		if (ret == AVERROR(EAGAIN)) {

			/* Out of data, grab the next packet from the demuxer that
			 * handles the audio file. */
			while ((ret = av_read_frame(st->fmt_ctx, stream_packet)) >= 0) {

				/* Check if it's an audio packet and send it to the decoder
				 * According to the docs the packet is always fully consumed
				 * and is owned by the caller, so we unref afterwards */
				if (stream_packet->stream_index == st->audio_stream_idx) {
					if ((ret = avcodec_send_packet(codec_ctx, stream_packet)) < 0) {
						st->ffmpeg_err = ret;
						ret = ERR_CODEC;
						av_packet_unref(stream_packet);
						goto cleanup;
					}
					av_packet_unref(stream_packet);
					/* We should have decoded avframes now */
					break;
				}

				/* Not an audio packet, unref and try next one */
				av_packet_unref(stream_packet);
			}

			if (ret < 0) {
				if (ret == AVERROR_EOF) {
					/* No more packets left on the stream, we need a new file
					 * Flush any pending avframes out of the decoder and retry */
					avcodec_send_packet(codec_ctx, NULL);
				} else {
					st->ffmpeg_err = ret;
					ret = ERR_CODEC;
					goto cleanup;
				}
			}

			continue;
		} else if (ret == AVERROR_EOF) {
			 /* No more avframes available on the decoder */
			 eof_reached = 1;
			 break;
		} else if (ret < 0) {
			st->ffmpeg_err = ret;
			ret = ERR_CODEC;
			goto cleanup;
		}


		/* Note that nb_samples here are samples per channel (so audio frames)
		 * but we use it for calculating the duration, not some buffer length. */
		total_samples += decoded_avframe->nb_samples;

		/* If we only want to decode file for testing, ignore the rest */
		if (!st->ebur128_ctx)
			continue;

		/* Got a new avframe to pass on to ebur128, if we need to convert it
		 * go through swr, or else pass it on to libebur128 directly */
		if (st->swr_ctx) {
			/* Check if the next call to swr_convert_frame will need extra frames
			 * for delay compensation, or we have any pending frames to flush out of
			 * the resampler. */
			 int64_t swr_delay = swr_get_delay(st->swr_ctx, codec_ctx->sample_rate);
			 if (swr_delay < 0) {
				st->ffmpeg_err = ret;
				ret = ERR_SWR;
				goto cleanup;
			 }

			/* Check if we have enough space on resampled_frame for the resampled output */
			uint32_t required_frames = swr_delay + decoded_avframe->nb_samples;
			size_t required_bytes = required_frames * codec_ctx->ch_layout.nb_channels * sizeof(float);
			size_t allocated_bytes = st->converted_avframe->buf[0]->size;
			if (required_bytes > allocated_bytes) {

				 /* Free up the current one */
				av_frame_unref(st->converted_avframe);

				/* Re-initialize/realloc st->converted_avframe */
				st->converted_avframe->nb_samples = required_frames;
				av_channel_layout_copy(&st->converted_avframe->ch_layout, &st->codec_ctx->ch_layout);
				st->converted_avframe->format = AV_SAMPLE_FMT_FLT;
				st->converted_avframe->sample_rate = codec_ctx->sample_rate;;

				 ret = av_frame_get_buffer(st->converted_avframe, 0);
				 if (ret < 0) {
					st->ffmpeg_err = ret;
					ret = ERR_NOMEM;
					goto cleanup;
				 }
			}

			if (decoded_avframe->nb_samples > 0)
				ret = swr_convert_frame(st->swr_ctx, st->converted_avframe, decoded_avframe);
			else
				ret = swr_convert_frame(st->swr_ctx, st->converted_avframe, NULL);

			if (ret < 0) {
				st->ffmpeg_err = ret;
				ret = ERR_SWR;
				goto cleanup;
			}
			processing_frame = st->converted_avframe;
		} else {
			processing_frame = decoded_avframe;
		}

		ret = ebur128_add_frames_float(st->ebur128_ctx,(float *)processing_frame->data[0],
					       processing_frame->nb_samples);
		if (ret != EBUR128_SUCCESS) {
			st->libebur128_err = ret;
			ret = ERR_EBUR128;
			goto cleanup;
		}
	}

	/* Calculate duration from samples - rounded to whole seconds */
	if (total_samples > 0 && track_info->sample_rate > 0) {
		uint32_t calculated_duration = (int)(((float)total_samples + 0.5f) /
						     (float)track_info->sample_rate);
		
		/* Calculate difference between calculated duration and duration from metadata */
		if (track_info->duration_secs > 0) {
			int diff = abs(calculated_duration - track_info->duration_secs);
			track_info->duration_diff = diff;
		} else
			track_info->duration_secs = calculated_duration;
	}
	/* total_samples -> samples per channel, so frames */
	track_info->total_frames = total_samples;

	/* We successfully decoded all frames and passed them on
	 * to libebur128 for analysis, read back the results. */
	if (!st->ebur128_ctx) {
		ret = ERR_OK;
		goto cleanup;
	}

	double loudness_d, lra_d;
	ret = ebur128_loudness_global(st->ebur128_ctx, &loudness_d);
	if (ret != EBUR128_SUCCESS) {
		st->libebur128_err = ret;
		ret = ERR_EBUR128;
		goto cleanup;
	}
	track_info->ebur128_iloud = loudness_d;

	/* Get the relative threshold */
	double relative_threshold;
	if (ebur128_relative_threshold(st->ebur128_ctx, &relative_threshold) == EBUR128_SUCCESS) {
		track_info->relative_threshold = relative_threshold;
	} else {
		track_info->relative_threshold = -70.0; /* Fallback to absolute threshold */
	}

	if (do_lra) {
		ret = ebur128_loudness_range(st->ebur128_ctx, &lra_d);
		if (ret != EBUR128_SUCCESS) {
			st->libebur128_err = ret;
			ret = ERR_EBUR128;
			goto cleanup;
		}
		track_info->ebur128_lra = (float) lra_d;
	}

	/* maximum sample peak across all channels */
	float max_sample_peak = 0.0f;
	for (int ch = 0; ch < codec_ctx->ch_layout.nb_channels; ch++) {
		 double sample_peak_d;
		 ret = ebur128_sample_peak(st->ebur128_ctx, ch, &sample_peak_d);
		 if (ret != EBUR128_SUCCESS) {
			st->libebur128_err = ret;
			ret = ERR_EBUR128;
			goto cleanup;
		}
 
		 float sample_peak = (float)sample_peak_d;
		 if (sample_peak > max_sample_peak) {
			 max_sample_peak = sample_peak;
		 }
	}
	track_info->sample_peak = max_sample_peak;
 
	/* Calculate ReplayGain 2 gain value (how much to adjust to reach reference level) */
	track_info->rg2_gain = RG2_REFERENCE - track_info->ebur128_iloud;

	ret = ERR_OK;

 cleanup:
	if (decoded_avframe)
		av_frame_free(&decoded_avframe);
	if (stream_packet)
		av_packet_free(&stream_packet);
	return ret;
}
#if 0
int main(int argc, char *argv[])
{
	struct aunlz_state st = {0};
	AunlzResults *track_info = &st.track_info;
	int do_decode = 1;
	int do_ebur128 = 1;
	int do_lra = 0;
	int ret = 0;

	if (argc < 2) {
		fprintf(stderr, "Usage: %s <input_file>\n", argv[0]);
		return 1;
	}

	const char *input_file = argv[1];

	ret = aunlz_init(&st, input_file, do_ebur128, do_lra);
	if (ret < 0)
		return ret;

	aunlz_fill_basic_info(&st);

	if (do_decode) {
		ret = aunlz_process(&st, do_lra);
		if (ret < 0)
			goto cleanup;
	}

	printf("File format: %s\n", track_info->format_name);
	printf("Sample rate: %i\n", track_info->sample_rate);
	printf("Bitrate: %i\n", track_info->bit_rate);
	printf("Bit depth: %i\n", track_info->bit_depth);
	printf("Duration (sec): %i\n", track_info->duration_secs);

	if (do_decode && do_ebur128) {
		printf("EBU R128 Loudness Metrics:\n");
		printf("  Integrated Loudness (I): %.2f LUFS\n", track_info->ebur128_iloud);
		if (do_lra)
			printf("  Loudness Range (LRA): %.2f LU\n", track_info->ebur128_lra);
		printf("  Maximum Sample Peak: %.6f\n", track_info->sample_peak);
		printf("  ReplayGain 2 Gain: %.2f dB\n", track_info->rg2_gain);
	}

 cleanup:
	aunlz_cleanup(&st);
	return ret;
}
#endif

/*************\
* PYTHON GLUE *
\*************/
static PyMethodDef aunalyzer_methods[];
static PyModuleDef aunalyzer_module;

/* AunlzResults object handling */
static void
AunlzResults_dealloc(AunlzResults *self)
{
	/* Free the format_name string if we've allocated it */
	if (self->format_name)
		PyMem_Free((void*)self->format_name);

	Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
AunlzResults_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
	AunlzResults *self;
	self = (AunlzResults *)type->tp_alloc(type, 0);
	if (self != NULL) {
		self->format_name = NULL;
		self->sample_rate = 0;
		self->bit_rate = 0;
		self->bit_depth = 0;
		self->duration_secs = 0;
		self->duration_diff = 0;
		self->total_frames = 0;
		self->ebur128_iloud = 0.0;
		self->relative_threshold = 0.0;
		self->ebur128_lra = 0.0f;
		self->sample_peak = 0.0f;
		self->rg2_gain = 0.0f;
	}
	return (PyObject *)self;
}

static int
AunlzResults_init(AunlzResults *self, PyObject *args, PyObject *kwds)
{
	/* Usually not needed since we're creating these objects in C code */
	return 0;
}

static PyMemberDef AunlzResults_members[] = {
	{"sample_rate", T_UINT, offsetof(AunlzResults, sample_rate), 0, "Sample rate in Hz"},
	{"bit_rate", T_UINT, offsetof(AunlzResults, bit_rate), 0, "Bit rate in bits/s"},
	{"bit_depth", T_UBYTE, offsetof(AunlzResults, bit_depth), 0, "Bit depth"},
	{"duration_secs", T_UINT, offsetof(AunlzResults, duration_secs), 0, "Duration in seconds"},
	{"duration_diff", T_UINT, offsetof(AunlzResults, duration_diff), 0, "Duration difference (metadata vs calculated)"},
	{"total_frames", T_ULONGLONG, offsetof(AunlzResults, total_frames), 0, "Total frames/samples analyzed"},
	{"ebur128_iloud", T_DOUBLE, offsetof(AunlzResults, ebur128_iloud), 0, "Integrated loudness (LUFS)"},
	{"relative_threshold", T_DOUBLE, offsetof(AunlzResults, relative_threshold), 0, "Relative threshold used gating (LUFS)"},
	{"ebur128_lra", T_FLOAT, offsetof(AunlzResults, ebur128_lra), 0, "Loudness range (LU)"},
	{"sample_peak", T_FLOAT, offsetof(AunlzResults, sample_peak), 0, "Maximum sample peak"},
	{"rg2_gain", T_FLOAT, offsetof(AunlzResults, rg2_gain), 0, "ReplayGain 2 gain adjustment (dB)"},
	{NULL}  /* Sentinel */
};

static PyObject *
AunlzResults_get_format_name(AunlzResults *self, void *closure)
{
	if (self->format_name == NULL)
		Py_RETURN_NONE;

	return PyUnicode_FromString(self->format_name);
}

static PyGetSetDef AunlzResults_getsetters[] = {
	{"format_name", (getter)AunlzResults_get_format_name, NULL, "Format name", NULL},
	{NULL}  /* Sentinel */
};

static PyTypeObject AunlzResultsType = {
	PyVarObject_HEAD_INIT(NULL, 0)
	.tp_name = "aunalyzer.AunlzResults",
	.tp_doc = "Audio track information and analysis results",
	.tp_basicsize = sizeof(AunlzResults),
	.tp_itemsize = 0,
	.tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
	.tp_new = AunlzResults_new,
	.tp_init = (initproc)AunlzResults_init,
	.tp_dealloc = (destructor)AunlzResults_dealloc,
	.tp_members = AunlzResults_members,
	.tp_getset = AunlzResults_getsetters,
};

/* Define a custom exception type for our module */
static PyObject *AunalyzerException;

/* The analyze function exposed to Python */
static PyObject *
analyze_audio(PyObject *self, PyObject *args, PyObject *kwargs)
{
	const char *filepath = NULL;
	int do_decode = 1;
	int do_ebur128 = 1;
	int do_lra = 0;
	struct aunlz_state st = {0};
	int ret = 0;
	AunlzResults *track_info_obj = NULL;
	static char *kwlist[] = {"filepath", "do_decode", "do_ebur128", "do_lra", NULL};

	if (!PyArg_ParseTupleAndKeywords(args, kwargs, "s|iii", kwlist, 
	    &filepath, &do_decode, &do_ebur128, &do_lra))
		return NULL;

	/* Initialize and process the audio file */
	ret = aunlz_init(&st, filepath, do_ebur128, do_lra);
	if (ret != ERR_OK)
		goto err;

	/* Fill format / decoder info on st.track_info */
	ret = aunlz_fill_basic_info(&st);
	if (ret != ERR_OK)
		goto err;

	/* Create a new AunlzResults Python object */
	track_info_obj = (AunlzResults*)PyObject_CallObject((PyObject*)&AunlzResultsType, NULL);
	if (!track_info_obj) {
		st.err = ERR_NOMEM;
		goto err;
	}

	/* Copy basic info to the new Python object */
	if (st.track_info.format_name) {
		size_t len = strlen(st.track_info.format_name);
		track_info_obj->format_name = PyMem_Malloc(len + 1);
		if (track_info_obj->format_name) {
			memcpy((void*)track_info_obj->format_name, st.track_info.format_name, len + 1);
		} else {
			st.err = ERR_NOMEM;
			goto err;
		}
	}

	track_info_obj->sample_rate = st.track_info.sample_rate;
	track_info_obj->bit_rate = st.track_info.bit_rate;
	track_info_obj->bit_depth = st.track_info.bit_depth;
	track_info_obj->duration_secs = st.track_info.duration_secs;

	if (!do_decode)
		goto skip_decode;

	ret = aunlz_process(&st, do_lra);
	if (ret != ERR_OK)
		goto err;

	track_info_obj->duration_diff = st.track_info.duration_diff;
	track_info_obj->total_frames = st.track_info.total_frames;
	track_info_obj->ebur128_iloud = st.track_info.ebur128_iloud;
	track_info_obj->relative_threshold = st.track_info.relative_threshold;
	track_info_obj->ebur128_lra = st.track_info.ebur128_lra;
	track_info_obj->sample_peak = st.track_info.sample_peak;
	track_info_obj->rg2_gain = st.track_info.rg2_gain;

 skip_decode:
	aunlz_cleanup(&st);
	return (PyObject*)track_info_obj;

 err:
	const char *error_msg = NULL;
	st.err = ret;
	error_msg = aunlz_get_combined_error_msg(&st);
	PyObject *exception_args = PyTuple_New(3);
	if (exception_args) {
		/* Add the error code as first element (integer) */
		PyTuple_SetItem(exception_args, 0, PyLong_FromLong(st.err));

		/* Add the error message as second element (string) */
		PyTuple_SetItem(exception_args, 1, PyUnicode_FromString(error_msg));

		/* Add the track_info as third element (or None) */
		if (track_info_obj) {
			/* Note: PyTuple_SetItem steals the reference, so we don't need to decref track_info_obj */
			PyTuple_SetItem(exception_args, 2, (PyObject*)track_info_obj);
		} else {
			Py_INCREF(Py_None);
			PyTuple_SetItem(exception_args, 2, Py_None);
		}

		/* Set the exception with the tuple */
		PyErr_SetObject(AunalyzerException, exception_args);
		Py_DECREF(exception_args);
	} else {
		/* If we couldn't create the tuple, fall back to a simple error message */
		Py_XDECREF(track_info_obj);
		PyErr_SetString(AunalyzerException, error_msg);
	}
	aunlz_cleanup(&st);
	return NULL;
}

static PyMethodDef aunalyzer_methods[] = {
	{"analyze_audio", (PyCFunction)analyze_audio, METH_VARARGS | METH_KEYWORDS,
	"Analyze an audio file and return track information.\n"
	"Parameters:\n"
	"  filepath (str): Path to the audio file\n"
	"  do_ebur128 (bool, optional): Calculate EBU R128 loudness measurements (default: True)\n"
	"  do_lra (bool, optional): Calculate loudness range (default: False)\n"
	"Returns:\n"
	"  AunlzResults: Object containing the analysis results"
	},
	{NULL, NULL, 0, NULL}  /* Sentinel */
};

static PyModuleDef aunalyzer_module = {
	PyModuleDef_HEAD_INIT,
	"_aunalyzer",
	"Python module for audio file analysis using FFmpeg and libebur128",
	-1,
	aunalyzer_methods,
	NULL, NULL, NULL, NULL
};

/* Module initialization function */
PyMODINIT_FUNC
PyInit__aunalyzer(void)
{
	PyObject *m;

	/* Initialize the AunlzResults type */
	if (PyType_Ready(&AunlzResultsType) < 0)
		return NULL;

	/* Create the module */
	m = PyModule_Create(&aunalyzer_module);
	if (m == NULL)
		return NULL;

	/* Add the AunlzResults type to the module */
	Py_INCREF(&AunlzResultsType);
	if (PyModule_AddObject(m, "AunlzResults", (PyObject *)&AunlzResultsType) < 0) {
		Py_DECREF(&AunlzResultsType);
		Py_DECREF(m);
		return NULL;
	}

	/* Create and add our custom exception */
	AunalyzerException = PyErr_NewException("aunalyzer.AunalyzerException", NULL, NULL);
	if (AunalyzerException == NULL) {
		Py_DECREF(&AunlzResultsType);
		Py_DECREF(m);
		return NULL;
	}

	Py_INCREF(AunalyzerException);
	if (PyModule_AddObject(m, "AunalyzerException", AunalyzerException) < 0) {
		Py_DECREF(AunalyzerException);
		Py_DECREF(&AunlzResultsType);
		Py_DECREF(m);
		return NULL;
	}

	/* Add error constants */
	PyModule_AddIntConstant(m, "ERR_OK", ERR_OK);
	PyModule_AddIntConstant(m, "ERR_NOMEM", ERR_NOMEM);
	PyModule_AddIntConstant(m, "ERR_NOFILE", ERR_NOFILE);
	PyModule_AddIntConstant(m, "ERR_NOSTREAM", ERR_NOSTREAM);
	PyModule_AddIntConstant(m, "ERR_NOCODEC", ERR_NOCODEC);
	PyModule_AddIntConstant(m, "ERR_FMT", ERR_FMT);
	PyModule_AddIntConstant(m, "ERR_CODEC_INIT", ERR_CODEC_INIT);
	PyModule_AddIntConstant(m, "ERR_CODEC", ERR_CODEC);
	PyModule_AddIntConstant(m, "ERR_EBUR128_INIT", ERR_EBUR128_INIT);
	PyModule_AddIntConstant(m, "ERR_EBUR128", ERR_EBUR128);
	PyModule_AddIntConstant(m, "ERR_SWR_INIT", ERR_SWR_INIT);
	PyModule_AddIntConstant(m, "ERR_SWR", ERR_SWR);

	return m;
}