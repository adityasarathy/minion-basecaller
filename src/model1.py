import tensorflow as tf
import numpy as np
from util import atrous_conv1d


max_reach = 32  # How many extra elements I have to fetch for convolutions
state_size = 50  # For RNN
out_classes = 5  # A,G,T,C plus LAST state for blank. Last due to CTC implementation

# For slicing input (due to data locality it's more efficient to keep as much data as possible on GPU, thus slicing):
begin = tf.placeholder(dtype=tf.int32, shape=[], name="begin")
length = 3  # Fixed due to dynamic_rnn

# TODO, once I have data pipeline in place
X = tf.placeholder(tf.float32, shape=(None, None, 1), name="X")
X_len = tf.placeholder(tf.int32, shape=(None,), name="X_len")
Y = tf.sparse_placeholder(tf.int32, shape=(None, None), name="Y")
Y_len = tf.placeholder(tf.int32, shape=(None,), name="Y_len")

batch_size = tf.shape(X)[0]
net = X
with tf.control_dependencies([
    tf.assert_less_equal(begin + length, tf.shape(X)[1], message="Cannot request that many elements from X"),
    tf.assert_non_negative(begin, message="Beginning slice must be >=0")
]):
    left = tf.maximum(0, begin - max_reach)
    right = tf.minimum(tf.shape(X)[1], begin + length + max_reach)

net = tf.slice(X, [0, left, 0], [-1, right - left, -1])
net.set_shape([None, None, 1])
for i, no_channel in zip([2, 4, 8, 16], [8, 16, 16, 16]):
    with tf.variable_scope("atrous_conv1d_%d" % i):
        print(net.get_shape())
        filter = tf.get_variable("W", shape=(3, net.get_shape()[-1], no_channel))
        bias = tf.get_variable("b", shape=(no_channel,))
        net = atrous_conv1d(net, filter, i) + bias
        net = tf.nn.relu(net)
net = tf.slice(net, [0, begin - left, 0], [-1, length, -1])

with tf.name_scope("RNN"):
    cell = tf.nn.rnn_cell.GRUCell(state_size)
    init_state = cell.zero_state(batch_size, dtype=tf.float32)
    seq_len = tf.maximum(0, X_len - begin)
    outputs, final_state = tf.nn.dynamic_rnn(cell, net, initial_state=init_state, sequence_length=seq_len)

with tf.variable_scope("Output"):
    outputs = tf.reshape(outputs, [-1, state_size])
    W = tf.get_variable("W", shape=[state_size, out_classes])
    b = tf.get_variable("b", shape=[out_classes])
    outputs = tf.matmul(outputs, W) + b
    logits = tf.reshape(outputs, [batch_size, length, out_classes])

yy = tf.slice(Y, [0, begin], [-1, length])
yy_len = tf.maximum(0, Y_len - begin)

loss = tf.nn.ctc_loss(logits, yy, yy_len, preprocess_collapse_repeated=False, ctc_merge_repeated=False)

predicted, predicted_logprob = tf.nn.ctc_beam_search_decoder(tf.transpose(logits, [1, 0, 2]), yy_len, merge_repeated=False)

pred = tf.cast(predicted[0], tf.int32)

optimizer = tf.train.AdamOptimizer()
train_op = optimizer.minimize(loss)
grads = optimizer.compute_gradients(loss)

if __name__ == "__main__":
    # Testing stuff
    sess = tf.Session()
    sess.run(tf.global_variables_initializer())
    gg = sess.run(logits, feed_dict={X: 1 + np.arange(14).reshape(2, 7, 1), begin: 3, X_len:[3, 4]})
    print(gg, gg.shape)
