#   Copyright 2021 The TensorFlow Authors. All Rights Reserved.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
"""Prepare TF.Examples for on-device recommendation model.

Following functions are included: 1) downloading raw data 2) processing to user
activity sequence and splitting to train/test data 3) convert to TF.Examples
and write in output location.

More information about the movielens dataset can be found here:
https://grouplens.org/datasets/movielens/
"""

import collections
import json
import os
import random
import re

from absl import app
from absl import flags
from absl import logging
import pandas as pd
import tensorflow as tf


FLAGS = flags.FLAGS
flags.DEFINE_string("data_dir", "/tmp",
                    "Path to download and store movielens data.")
flags.DEFINE_string("output_dir", None,
                    "Path to the directory of output files.")
flags.DEFINE_bool("build_vocabs", True,
                  "If yes, generate movie feature vocabs.")
flags.DEFINE_integer("min_timeline_length", 3,
                     "The minimum timeline length to construct examples.")
flags.DEFINE_integer("max_context_length", 10,
                     "The maximum length of user context history.")
flags.DEFINE_integer("max_context_movie_genre_length", 10,
                     "The maximum length of user context history.")
flags.DEFINE_integer("min_rating", None,
                     "Minimum rating of movie that will be used to in "
                     "training data")
flags.DEFINE_float("train_data_fraction", 0.9, "Fraction of training data.")

# Permalinks to download movielens data.
MOVIELENS_1M_URL = "http://files.grouplens.org/datasets/movielens/ml-1m.zip"
MOVIELENS_ZIP_FILENAME = "ml-1m.zip"
MOVIELENS_EXTRACTED_DIR = "ml-1m"
RATINGS_FILE_NAME = "ratings.dat"
MOVIES_FILE_NAME = "movies.dat"
RATINGS_DATA_COLUMNS = ["UserID", "MovieID", "Rating", "Timestamp"]
MOVIES_DATA_COLUMNS = ["MovieID", "Title", "Genres"]
OUTPUT_TRAINING_DATA_FILENAME = "train_movielens_1m.tfrecord"
OUTPUT_TESTING_DATA_FILENAME = "test_movielens_1m.tfrecord"
OUTPUT_MOVIE_VOCAB_FILENAME = "movie_vocab.json"
OUTPUT_MOVIE_YEAR_VOCAB_FILENAME = "movie_year_vocab.txt"
OUTPUT_MOVIE_GENRE_VOCAB_FILENAME = "movie_genre_vocab.txt"
OUTPUT_MOVIE_TITLE_UNIGRAM_VOCAB_FILENAME = "movie_title_unigram_vocab.txt"
OUTPUT_MOVIE_TITLE_BIGRAM_VOCAB_FILENAME = "movie_title_bigram_vocab.txt"
PAD_MOVIE_ID = 0
PAD_RATING = 0.0
PAD_MOVIE_YEAR = 0
UNKNOWN_STR = "UNK"


class MovieInfo(
    collections.namedtuple(
        "MovieInfo", ["movie_id", "timestamp", "rating", "title", "genres"])):
  """Data holder of basic information of a movie."""
  __slots__ = ()

  def __new__(cls,
              movie_id=PAD_MOVIE_ID,
              timestamp=0,
              rating=PAD_RATING,
              title="",
              genres=""):
    return super(MovieInfo, cls).__new__(cls, movie_id, timestamp, rating,
                                         title, genres)


def download_and_extract_data(data_directory, url=MOVIELENS_1M_URL):
  """Download and extract zip containing MovieLens data to a given directory.

  Args:
    data_directory: Local path to extract dataset to.
    url: Direct path to MovieLens dataset .zip file. See constants above for
      examples.

  Returns:
    Downloaded and extracted data file directory.
  """
  path_to_zip = tf.keras.utils.get_file(
      fname=MOVIELENS_ZIP_FILENAME,
      origin=url,
      extract=True,
      cache_dir=data_directory)
  extracted_file_dir = os.path.join(
      os.path.dirname(path_to_zip), MOVIELENS_EXTRACTED_DIR)
  return extracted_file_dir


def read_data(data_directory):
  """Read movielens ratings.dat and movies.dat file into dataframe."""
  ratings_df = pd.read_csv(
      os.path.join(data_directory, RATINGS_FILE_NAME),
      sep="::",
      names=RATINGS_DATA_COLUMNS)
  ratings_df["Timestamp"] = ratings_df["Timestamp"].apply(int)
  if FLAGS.min_rating is not None:
    ratings_df = ratings_df[ratings_df["Rating"] >= FLAGS.min_rating]
  movies_df = pd.read_csv(
      os.path.join(data_directory, MOVIES_FILE_NAME),
      sep="::",
      names=MOVIES_DATA_COLUMNS)
  return ratings_df, movies_df


def convert_to_timelines(ratings_df):
  """Convert ratings data to user."""
  timelines = collections.defaultdict(list)
  movie_counts = collections.Counter()
  for user_id, movie_id, rating, timestamp in ratings_df.values:
    timelines[user_id].append(MovieInfo(
        movie_id=movie_id, timestamp=int(timestamp), rating=rating))
    movie_counts[movie_id] += 1
  # Sort per-user timeline by timestamp
  for (user_id, context) in timelines.items():
    context.sort(key=lambda x: x.timestamp)
    timelines[user_id] = context
  return timelines, movie_counts


def generate_movies_dict(movies_df):
  """Generates movies dictionary from movies dataframe."""
  movies_dict = {
      movie_id: MovieInfo(movie_id=movie_id, title=title, genres=genres)
      for movie_id, title, genres in movies_df.values
  }
  movies_dict[0] = MovieInfo()
  return movies_dict


def extract_year_from_title(title):
  year = re.search(r"\((\d{4})\)", title)
  if year:
    return int(year.group(1))
  return 0


def generate_feature_of_movie_years(movies_dict, movies):
  """Extracts year feature for movies from movie title."""
  return [
      extract_year_from_title(movies_dict[movie.movie_id].title)
      for movie in movies
  ]


def generate_movie_genres(movies_dict, movies):
  """Create a feature of the genre of each movie.

  Save genre as a feature for the movies.

  Args:
    movies_dict: Dict of movies, keyed by movie_id with value of (title, genre)
    movies: list of movies to extract genres.

  Returns:
    movie_genres: list of genres of all input movies.
  """
  movie_genres = []
  for movie in movies:
    if not movies_dict[movie.movie_id].genres:
      continue
    genres = [tf.compat.as_bytes(genre)
              for genre in movies_dict[movie.movie_id].genres.split("|")]
    movie_genres.extend(genres)

  return movie_genres


def _pad_or_truncate_movie_feature(feature, max_len, pad_value):
  feature.extend([pad_value for _ in range(max_len - len(feature))])
  return feature[:max_len]


def generate_examples_from_single_timeline(
    timeline,
    movies_dict,
    max_context_len=100,
    max_context_movie_genre_len=320):
  """Generate TF examples from a single user timeline.

  Generate TF examples from a single user timeline. Timeline with length less
  than minimum timeline length will be skipped. And if context user history
  length is shorter than max_context_len, features will be padded with default
  values.

  Args:
    timeline: The timeline to generate TF examples from.
    movies_dict: Dictionary of all MovieInfos.
    max_context_len: The maximum length of the context. If the context history
      length is less than max_context_length, features will be padded with
      default values.
    max_context_movie_genre_len: The length of movie genre feature.

  Returns:
    examples: Generated examples from this single timeline.
  """
  examples = []
  for label_idx in range(1, len(timeline)):
    start_idx = max(0, label_idx - max_context_len)
    context = timeline[start_idx:label_idx]
    # Pad context with out-of-vocab movie id 0.
    while len(context) < max_context_len:
      context.append(MovieInfo())
    label_movie_id = int(timeline[label_idx].movie_id)
    context_movie_id = [int(movie.movie_id) for movie in context]
    context_movie_rating = [movie.rating for movie in context]
    context_movie_year = generate_feature_of_movie_years(movies_dict, context)
    context_movie_genres = generate_movie_genres(movies_dict, context)
    context_movie_genres = _pad_or_truncate_movie_feature(
        context_movie_genres, max_context_movie_genre_len,
        tf.compat.as_bytes(UNKNOWN_STR))
    feature = {
        "context_movie_id":
            tf.train.Feature(
                int64_list=tf.train.Int64List(value=context_movie_id)),
        "context_movie_rating":
            tf.train.Feature(
                float_list=tf.train.FloatList(value=context_movie_rating)),
        "context_movie_genre":
            tf.train.Feature(
                bytes_list=tf.train.BytesList(value=context_movie_genres)),
        "context_movie_year":
            tf.train.Feature(
                int64_list=tf.train.Int64List(value=context_movie_year)),
        "label_movie_id":
            tf.train.Feature(
                int64_list=tf.train.Int64List(value=[label_movie_id]))
    }
    tf_example = tf.train.Example(features=tf.train.Features(feature=feature))
    examples.append(tf_example)

  return examples


def generate_examples_from_timelines(timelines,
                                     movies_df,
                                     min_timeline_len=3,
                                     max_context_len=100,
                                     max_context_movie_genre_len=320,
                                     train_data_fraction=0.9,
                                     random_seed=None,
                                     shuffle=True):
  """Convert user timelines to tf examples.

  Convert user timelines to tf examples by adding all possible context-label
  pairs in the examples pool.

  Args:
    timelines: The user timelines to process.
    movies_df: The dataframe of all movies.
    min_timeline_len: The minimum length of timeline. If the timeline length is
      less than min_timeline_len, empty examples list will be returned.
    max_context_len: The maximum length of the context. If the context history
      length is less than max_context_length, features will be padded with
      default values.
    max_context_movie_genre_len: The length of movie genre feature.
    train_data_fraction: Fraction of training data.
    random_seed: Seed for randomization.
    shuffle: Whether to shuffle the examples before splitting train and test
      data.

  Returns:
    train_examples: TF example list for training.
    test_examples: TF example list for testing.
  """
  examples = []
  movies_dict = generate_movies_dict(movies_df)
  for timeline in timelines.values():
    if len(timeline) < min_timeline_len:
      continue
    single_timeline_examples = generate_examples_from_single_timeline(
        timeline=timeline,
        movies_dict=movies_dict,
        max_context_len=max_context_len,
        max_context_movie_genre_len=max_context_movie_genre_len)
    examples.extend(single_timeline_examples)
  # Split the examples into train, test sets.
  if shuffle:
    random.seed(random_seed)
    random.shuffle(examples)
  last_train_index = round(len(examples) * train_data_fraction)

  train_examples = examples[:last_train_index]
  test_examples = examples[last_train_index:]
  return train_examples, test_examples


def generate_movie_feature_vocabs(movies_df, movie_counts):
  """Generate vocabularies for movie features.

  Generate vocabularies for movie features (movie_id, genre, year), sorted by
  usage count. Vocab id 0 will be reserved for default padding value.

  Args:
    movies_df: Dataframe for movies.
    movie_counts: Counts that each movie is rated.

  Returns:
    movie_id_vocab: List of all movie ids paired with movie usage count, and
      sorted by counts.
    movie_genre_vocab: List of all movie genres, sorted by genre usage counts.
    movie_year_vocab: List of all movie years, sorted by year usage counts.
  """
  movie_id_vocab = []
  movie_genre_counter = collections.Counter()
  movie_year_counter = collections.Counter()
  for movie_id, title, genres in movies_df.values:
    count = movie_counts.get(movie_id) or 0
    movie_id_vocab.append([movie_id, count])
    year = extract_year_from_title(title)
    movie_year_counter[year] += 1
    for genre in genres.split("|"):
      movie_genre_counter[genre] += 1

  movie_id_vocab.sort(key=lambda x: x[1], reverse=True)
  movie_year_vocab = [0] + [x for x, _ in movie_year_counter.most_common()]
  movie_genre_vocab = [UNKNOWN_STR
                      ] + [x for x, _ in movie_genre_counter.most_common()]

  return (movie_id_vocab, movie_year_vocab, movie_genre_vocab)


def write_tfrecords(tf_examples, filename):
  """Write tf examples to tfrecord file."""
  with tf.io.TFRecordWriter(filename) as file_writer:
    for example in tf_examples:
      file_writer.write(example.SerializeToString())


def write_vocab_json(movie_id_vocab, filename):
  """Write generated movie vocabulary to specified file."""
  with open(filename, "w", encoding="utf-8") as jsonfile:
    json.dump(movie_id_vocab, jsonfile, indent=2)


def write_vocab_txt(vocab, filename):
  with open(filename, "w", encoding="utf-8") as f:
    for item in vocab:
      f.write(str(item) + "\n")


def main(_):
  data_dir = FLAGS.data_dir
  if not tf.io.gfile.exists(data_dir):
    tf.io.gfile.makedirs(data_dir)

  logging.info("Downloading and extracting data.")
  extracted_file_dir = download_and_extract_data(data_directory=data_dir)
  logging.info("Reading data to dataframes.")
  ratings_df, movies_df = read_data(data_directory=extracted_file_dir)
  logging.info("Generating train and test examples.")
  timelines, movie_counts = convert_to_timelines(ratings_df)
  train_examples, test_examples = generate_examples_from_timelines(
      timelines=timelines,
      movies_df=movies_df,
      min_timeline_len=FLAGS.min_timeline_length,
      max_context_len=FLAGS.max_context_length,
      max_context_movie_genre_len=FLAGS.max_context_movie_genre_length,
      train_data_fraction=FLAGS.train_data_fraction)

  if not tf.io.gfile.exists(FLAGS.output_dir):
    tf.io.gfile.makedirs(FLAGS.output_dir)
  write_tfrecords(
      tf_examples=train_examples,
      filename=os.path.join(FLAGS.output_dir, OUTPUT_TRAINING_DATA_FILENAME))
  write_tfrecords(
      tf_examples=test_examples,
      filename=os.path.join(FLAGS.output_dir, OUTPUT_TESTING_DATA_FILENAME))

  if FLAGS.build_vocabs:
    (movie_id_vocab, movie_year_vocab, movie_genre_vocab) = (
        generate_movie_feature_vocabs(
            movies_df=movies_df, movie_counts=movie_counts))
    write_vocab_json(
        movie_id_vocab=movie_id_vocab,
        filename=os.path.join(FLAGS.output_dir, OUTPUT_MOVIE_VOCAB_FILENAME))

    for vocab, filename in zip([
        movie_year_vocab, movie_genre_vocab
    ], [
        OUTPUT_MOVIE_YEAR_VOCAB_FILENAME, OUTPUT_MOVIE_GENRE_VOCAB_FILENAME
    ]):
      write_vocab_txt(vocab, filename=os.path.join(FLAGS.output_dir, filename))


if __name__ == "__main__":
  app.run(main)
