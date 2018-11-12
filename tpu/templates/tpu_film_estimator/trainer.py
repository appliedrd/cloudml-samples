# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import argparse
import numpy as np
import tensorflow as tf
from tensorflow.contrib import summary

n_classes = 10

# Making the filter sizes a global variable so it's eaiser to coordinate
# between the modulation sub-network and the convolutional classifier
# sub-network.
filter_sizes = [32, 64]

# For each filter/feature map, we will apply a linear modulation.
n_film = sum(filter_sizes)

# ## Feature-wise Linear Modulation Layer
#
# For details, see [FiLM: Visual Reasoning with a General Conditioning Layer](https://arxiv.org/abs/1709.07871).
#


class FeaturewiseLinearModulationLayer(tf.layers.Layer):
    def call(self, input_, gamma, beta):
        # The user is responsible for having the correct shapes
        return gamma * input_ + beta


# ## The model function
# 
#

def model_fn(features, labels, mode, params):
    x = features['x']
    modulation_data = features['modulation_data']
    onehot_labels = tf.one_hot(labels, n_classes)

    global_step = tf.train.get_global_step()

    # In this sample we use dense layers for the modulation sub-network.
    # Its output has shape (batch_size, 2 * n_film) since each FiLM layer has
    # two parameters.
    modulation_hidden = tf.layers.dense(modulation_data, 128, activation=tf.nn.relu)

    # We want to allow negative modulation parameters. 
    # Here we just use the linear activation.
    modulation_parameters = tf.layers.dense(modulation_hidden, 2 * n_film)

    all_gamma = modulation_parameters[:, :n_film]
    all_beta = modulation_parameters[:, n_film:]

    # Convolutional layers for the label classifier.
    filter_0 = filter_sizes[0]
    conv_0 = tf.layers.conv2d(x, filters=filter_0, kernel_size=(3, 3))

    # Apply FiLM before the ReLU activation.
    # Reshape the modulation parameters manually.
    gamma_0 = all_gamma[:, None, None, :filter_0]
    beta_0 = all_beta[:, None, None, :filter_0]
    filmed_conv_0 = FeaturewiseLinearModulationLayer()(conv_0, gamma_0, beta_0)

    conv_out_0 = tf.nn.relu(filmed_conv_0)

    # Do the same for the next convolutional block
    filter_1 = filter_sizes[1]
    conv_1 = tf.layers.conv2d(conv_out_0, filters=filter_1, kernel_size=(3, 3))

    gamma_1 = all_gamma[:, None, None, -filter_1:]
    beta_1 = all_beta[:, None, None, -filter_1:]
    filmed_conv_1 = FeaturewiseLinearModulationLayer()(conv_1, gamma_1, beta_1)

    conv_out_1 = tf.nn.relu(filmed_conv_1)

    # Fully connected logits output
    flattened = tf.reshape(conv_out_1, (params['train_batch_size'], -1))
    label_classification_logits = tf.layers.dense(flattened, n_classes)

    predictions = tf.nn.softmax(label_classification_logits)
    loss = None
    train_op = None

    if mode == tf.estimator.ModeKeys.TRAIN:
        # define loss
        loss = tf.losses.softmax_cross_entropy(
            onehot_labels=onehot_labels,
            logits=label_classification_logits
        )

        # define train_op
        optimizer = tf.train.RMSPropOptimizer(learning_rate=0.05)

        # wrapper to make the optimizer work with TPUs
        if params['use_tpu']:
            optimizer = tf.contrib.tpu.CrossShardOptimizer(optimizer)

        train_op = optimizer.minimize(loss, global_step=global_step)

    if params['use_tpu']:
        # TPU version of EstimatorSpec
        return tf.contrib.tpu.TPUEstimatorSpec(
            mode=mode,
            predictions=predictions,
            loss=loss,
            train_op=train_op)
    else:
        return tf.estimator.EstimatorSpec(
            mode=mode,
            predictions=predictions,
            loss=loss,
            train_op=train_op)


# ## The input function
#

def train_input_fn(params={}):
    # labaled image data
    x = np.random.rand(100, 28, 28, 3)
    y = np.random.randint(0, n_classes, 100)

    # additional input data for modulation
    modulation_data = np.random.rand(100, 5)

    x_tensor = tf.constant(x, dtype=tf.float32)
    y_tensor = tf.constant(y, dtype=tf.int32)
    modulation_data_tensor = tf.constant(modulation_data, dtype=tf.float32)

    # make a dataset
    dataset = tf.data.Dataset.from_tensor_slices((x_tensor, y_tensor, modulation_data_tensor))

    # TPUEstimator passes params when calling input_fn
    batch_size = params.get('batch_size', 16)

    dataset = dataset.repeat().shuffle(32).batch(batch_size, drop_remainder=True)

    # TPUs need to know all dimensions when the graph is built
    # Datasets know the batch size only when the graph is run
    def set_shapes_and_format(x, y, modulation_data):
        x_shape = x.get_shape().merge_with([batch_size, None, None, None])
        y_shape = y.get_shape().merge_with([batch_size])
        modulation_data_shape = modulation_data.get_shape().merge_with([batch_size, None])

        x.set_shape(x_shape)
        y.set_shape(y_shape)
        modulation_data.set_shape(modulation_data_shape)

        # Also format the dataset with a dict for features
        features = {'x': x, 'modulation_data': modulation_data}
        labels = y

        return features, labels

    dataset = dataset.map(set_shapes_and_format)
    dataset = dataset.prefetch(tf.contrib.data.AUTOTUNE)

    return dataset


def main(args):
    # pass the args as params so the model_fn can use
    # the TPU specific args
    params = vars(args)

    if args.use_tpu:
        # additional configs required for using TPUs
        tpu_cluster_resolver = tf.contrib.cluster_resolver.TPUClusterResolver(args.tpu)
        tpu_config = tf.contrib.tpu.TPUConfig(
            num_shards=8, # using Cloud TPU v2-8
            iterations_per_loop=args.save_checkpoints_steps)

        # use the TPU version of RunConfig
        config = tf.contrib.tpu.RunConfig(
            cluster=tpu_cluster_resolver,
            model_dir=args.model_dir,
            tpu_config=tpu_config,
            save_checkpoints_steps=args.save_checkpoints_steps,
            save_summary_steps=100)

        # TPUEstimator
        estimator = tf.contrib.tpu.TPUEstimator(
            model_fn=model_fn,
            config=config,
            params=params,
            train_batch_size=args.train_batch_size,
            eval_batch_size=32, # FIXME
            export_to_tpu=False)
    else:
        config = tf.estimator.RunConfig(model_dir=args.model_dir)

        estimator = tf.estimator.Estimator(
            model_fn,
            config=config,
            params=params)

    estimator.train(train_input_fn, max_steps=args.max_steps)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '--model-dir',
        type=str,
        default='/tmp/tpu-template')
    parser.add_argument(
        '--max-steps',
        type=int,
        default=1000)
    parser.add_argument(
        '--train-batch-size',
        type=int,
        default=16)
    parser.add_argument(
        '--save-checkpoints-steps',
        type=int,
        default=100)
    parser.add_argument(
        '--use-tpu',
        action='store_true')
    parser.add_argument(
        '--tpu',
        default=None)

    parser.add_argument(
        '--gr-weight',
        default=1.0,
        help='The weight for gradient reversal.')
    parser.add_argument(
        '--lambda',
        default=1.0,
        help='The regularization factor.')

    args, _ = parser.parse_known_args()

    # colab.research.google.com specific
    import sys
    if 'google.colab' in sys.modules:
        import json
        import os

        # TODO(user): change this
        args.model_dir = 'gs://your-gcs-bucket'

        # When connected to the TPU runtime
        if 'COLAB_TPU_ADDR' in os.environ:
            tpu_grpc = 'grpc://{}'.format(os.environ['COLAB_TPU_ADDR'])

            args.tpu = tpu_grpc
            args.use_tpu = True

            # Upload credentials to the TPU
            with tf.Session(tpu_grpc) as sess:
                data = json.load(open('/content/adc.json'))
                tf.contrib.cloud.configure_gcs(sess, credentials=data)

    main(args)
