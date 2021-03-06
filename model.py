import numpy as np
import os, time, sys
import tensorflow as tf
from tensorflow.contrib.rnn import LSTMCell
from tensorflow.contrib.crf import crf_log_likelihood
from tensorflow.contrib.crf import viterbi_decode
from tensorflow.python.ops.array_ops import ops
from data import pad_sequences, batch_yield
from utils import get_logger
from eval import conlleval


class BiLSTM_CRF(object):
    def __init__(self, args, embeddings, tag2label, vocab, paths, config):
        self.batch_size = args.batch_size
        self.epoch_num = args.epoch
        self.hidden_dim = args.hidden_dim
        self.embeddings = embeddings
        self.CRF = args.CRF
        self.update_embedding = args.update_embedding
        self.dropout_keep_prob = args.dropout
        self.optimizer = args.optimizer
        self.lr = args.lr
        self.clip_grad = args.clip
        self.tag2label = tag2label
        self.num_tags = len(tag2label)
        self.vocab = vocab
        self.shuffle = args.shuffle
        self.model_path = paths['model_path']
        self.summary_path = paths['summary_path']
        self.logger = get_logger(paths['log_path'])
        self.result_path = paths['result_path']
        self.config = config
        self.max_len=300
        self.vectors_len=100#词向量的维度！！

    def build_graph(self):
        self.add_placeholders()
        self.lookup_layer_op()
        self.biLSTM_layer_op()
        self.softmax_pred_op()
        self.loss_op()
        self.trainstep_op()
        self.init_op()

    def add_placeholders(self):
        #word_ids  shape=[batch_size,sentence_length]
        self.word_ids = tf.placeholder(tf.int32, shape=[None, None], name="word_ids")
        # labels   shape=[batch_size,sentence_length]
        self.labels = tf.placeholder(tf.int32, shape=[None, None], name="labels")
        # vector   shape=[batch_size,sentence_length]
        self.vectors = tf.placeholder(tf.float32, shape=[None, None,None], name="vectors")
        #sequence_lengths   shape=[batch_size]
        self.sequence_lengths = tf.placeholder(dtype=tf.int32, shape=[None], name="sequence_lengths")
        self.dropout_pl = tf.placeholder(dtype=tf.float32, shape=[], name="dropout")
        self.lr_pl = tf.placeholder(dtype=tf.float32, shape=[], name="lr_pl")

    def lookup_layer_op(self):
        with tf.variable_scope("words"):
            _word_embeddings = tf.Variable(self.embeddings,
                                           dtype=tf.float32,
                                           trainable=self.update_embedding,
                                           name="_word_embeddings")
            word_embeddings = tf.nn.embedding_lookup(params=_word_embeddings,
                                                     ids=self.word_ids,
                                                     name="word_embeddings")
        self.word_embeddings =  tf.nn.dropout(word_embeddings, self.dropout_pl)

    def biLSTM_layer_op(self):
        with tf.variable_scope("bi-lstm_attention"):
            cell_fw = LSTMCell(self.hidden_dim)
            cell_bw = LSTMCell(self.hidden_dim)
            (output_fw_seq, output_bw_seq), _ = tf.nn.bidirectional_dynamic_rnn(
                cell_fw=cell_fw,
                cell_bw=cell_bw,
                inputs=self.word_embeddings,
                sequence_length=self.sequence_lengths,dtype=tf.float32)
            output = tf.concat([output_fw_seq, output_bw_seq], axis=-1)
            #此时 output的 shape{batch_size, sentence, hidden_dim]
            with tf.variable_scope("score"):
                x1=tf.concat([self.word_embeddings, self.word_embeddings], axis=-1)             #batch_size, sentence, hidden_dim
                x1=tf.expand_dims(x1, -1)           #batch_size, sentence hidden_dim  1
                ones1=x1*0+1                        #batch_size, sentence hidden_dim  1
                x1=tf.transpose(x1,[0,1,3,2]) #batch_size, sentence   1 hidden_dim  
                x1=tf.matmul(ones1,x1)    #矩阵乘法   #batch_size,  sentence hidden_dim hidden_dim
                x2=tf.concat([self.word_embeddings, self.word_embeddings], axis=-1)             #batch_size, sentence, hidden_dim
                x2=tf.expand_dims(x2, -1)           #batch_size, sentence hidden_dim  1
                ones2=x2*0+1                        #batch_size, sentence hidden_dim  1
                ones2=tf.transpose(ones2,[0,1,3,2]) #batch_size, sentence   1 hidden_dim
                x2=tf.matmul(x2,ones2)   #矩阵乘法    #batch_size,  sentence hidden_dim hidden_dim
                score=x1-x2                          #batch_size,  sentence hidden_dim hidden_dim
                score=tf.transpose(score,[0,2,3,1]) #batch_size, hidden_dim hidden_dim sentence 
                score=tf.square(score)      #平方
                score=tf.reduce_sum(score,3)#降低最后一个维度 #batch_size, hidden_dim hidden_dim
                score=tf.sqrt(score)#开根号
                score=tf.matmul(tf.transpose(score,[0,2,1]),score,name=None) #矩阵乘法 #batch_size,hidden_dim 
                # print("score:    ",score.shape)#score后两个维度都是hiddensize

            with tf.variable_scope("alpha"):

                w_a=score*0+0.01
                score_w=tf.multiply(score, w_a)     #点乘     #batch_size,hidden_dim ,hidden_dim
                score_w_e=tf.exp(score_w)           #加权重    #batch_size,hidden_dim ,hidden_dim
                sum_score=tf.reduce_sum(score_w_e, 2) #求和分母 #batch_size,hidden_dim 
                sum_score=tf.expand_dims(sum_score, -1)#拓展维度#batch_size,hidden_dim ,1
                sum_score = tf.tile(sum_score, multiples=[1, 1, 2*self.hidden_dim])#batch_size,hidden_dim ,hidden_dim
                alpha=tf.divide(score_w_e,sum_score,name=None)#batch_size,hidden_dim ,hidden_dim 
            with tf.variable_scope("g"):
                alpha=tf.reduce_sum(alpha, 2) #求和     #batch_size,  hidden_dim
                output=tf.transpose(output,[1,0,2])     #sentence, batch_size, hidden_dim
                g=tf.multiply(alpha, output)#    #点乘  
                output=tf.transpose(output,[1,0,2])     #batch_size,sentence, hidden_dim
                g=tf.transpose(g,[1,0,2])               #batch_size,sentence, hidden_dim

            with tf.variable_scope("z"):
                g_h = tf.concat([g,output], 1)          #batch_size, sentence,  hidden_dim
                # w_g = tf.get_variable(name="w_g",
                #                 shape=[self.batch_size,300,4*self.hidden_dim],
                #                 initializer=tf.contrib.layers.xavier_initializer(),
                #                 dtype=tf.float32)
                w_g=g_h*0+0.001
                # w_g=tf.transpose(w_g,[0,2,1])
                z = tf.tanh(tf.multiply(g_h, w_g))    #点乘
                w_e=z*0+0.001
                e = tf.tanh(tf.multiply(z, w_e))    #点乘
            # #output的shape [batch_size,sentence,2*hidden_size]
            output = tf.nn.dropout(output, self.dropout_pl)

            output = tf.concat([output, self.vectors], axis=-1)#############################
# # 搞清楚输出到shape和cnn卷机后到shape

#         with tf.name_scope("cnn"):
#             # CNN layer
#             conv = tf.layers.conv1d(output, 256, 5, name='conv')
#             # global max pooling layer
#             gmp = tf.reduce_max(conv, reduction_indices=[1], name='gmp')
        with tf.variable_scope("proj_attention"):
            output1=output
            s = tf.shape(output1)
            # print(aaa.dtype)
            # print(s_numpy.dtype)
            # print(type(s[2]))

            W = tf.get_variable(name="W",
                                shape=[2*self.hidden_dim+self.vectors_len, self.num_tags],
                                initializer=tf.contrib.layers.xavier_initializer(),
                                dtype=tf.float32)
            b = tf.get_variable(name="b",
                                shape=[self.num_tags],
                                initializer=tf.zeros_initializer(),
                                dtype=tf.float32)
            #此时output的shape{batch_size*sentence,2*hidden_dim]
            output1 = tf.reshape(output1, [-1, s[2]])
            #pred的shape为[batch_size*sentence,num_classes]
            pred = tf.matmul(output1, W) + b
            #logits的shape为[batch,sentence,num_classes]
            self.logits = tf.reshape(pred, [-1, s[1], self.num_tags])
            # print("self.logits:   ",self.logits.shape)


    def loss_op(self):
        if self.CRF:
            #返回损失loss，和转移矩阵，转移矩阵的维度为[num_classes,num_classes]
            log_likelihood, self.transition_params = crf_log_likelihood(inputs=self.logits,
                                                                   tag_indices=self.labels,
                                                                   sequence_lengths=self.sequence_lengths)
            self.loss = -tf.reduce_mean(log_likelihood)

        else:
            #losses的shape为：[batch,sentence]  与labels的维度一致
            losses = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=self.logits,
                                                                    labels=self.labels)
            #sequence_length的维度为[batch_size]
            #mask的shape的维度为[batch,sentence]
            mask = tf.sequence_mask(self.sequence_lengths)
            #losses的shape为[batch]
            losses = tf.boolean_mask(losses, mask)
            #此时loss为一个值
            self.loss = tf.reduce_mean(losses)

        tf.summary.scalar("loss", self.loss)

    def softmax_pred_op(self):
        if not self.CRF:
            #logits的shape为[batch, sentence, num_classes]
            #labels_softmax_ 的shape为[batch,sentence]
            self.labels_softmax_ = tf.argmax(self.logits, axis=-1)
            #tf.cast为转换类型函数，转换参数设置的int32类型
            self.labels_softmax_ = tf.cast(self.labels_softmax_, tf.int32)

    def trainstep_op(self):
        with tf.variable_scope("train_step"):
            self.global_step = tf.Variable(0, name="global_step", trainable=False)
            if self.optimizer == 'Adam':
                optim = tf.train.AdamOptimizer(learning_rate=self.lr_pl)
            elif self.optimizer == 'Adadelta':
                optim = tf.train.AdadeltaOptimizer(learning_rate=self.lr_pl)
            elif self.optimizer == 'Adagrad':
                optim = tf.train.AdagradOptimizer(learning_rate=self.lr_pl)
            elif self.optimizer == 'RMSProp':
                optim = tf.train.RMSPropOptimizer(learning_rate=self.lr_pl)
            elif self.optimizer == 'Momentum':
                optim = tf.train.MomentumOptimizer(learning_rate=self.lr_pl, momentum=0.9)
            elif self.optimizer == 'SGD':
                optim = tf.train.GradientDescentOptimizer(learning_rate=self.lr_pl)
            else:
                optim = tf.train.GradientDescentOptimizer(learning_rate=self.lr_pl)

            grads_and_vars = optim.compute_gradients(self.loss)
            grads_and_vars_clip = [[tf.clip_by_value(g, -self.clip_grad, self.clip_grad), v] for g, v in grads_and_vars]
            self.train_op = optim.apply_gradients(grads_and_vars_clip, global_step=self.global_step)

    def init_op(self):
        self.init_op = tf.global_variables_initializer()

    def add_summary(self, sess):
        """

        :param sess:
        :return:
        """
        #将所有要在tensorb里的变量联合merge起来
        self.merged = tf.summary.merge_all()
        #用filewriter将这些graph，event放到一个路径中，到时候tensorb —-logdir="./路径"即可
        self.file_writer = tf.summary.FileWriter(self.summary_path, sess.graph)

    def train(self, train, dev):
        """

        :param train:
        :param dev:
        :return:
        """
        saver = tf.train.Saver(tf.global_variables())

        with tf.Session(config=self.config) as sess:
            sess.run(self.init_op)
            self.add_summary(sess)

            for epoch in range(self.epoch_num):
                self.run_one_epoch(sess, train, dev, self.tag2label, epoch, saver)

    def test(self, test):
        saver = tf.train.Saver()
        with tf.Session(config=self.config) as sess:
            self.logger.info('=========== testing ===========')
            saver.restore(sess, self.model_path)
            label_list, seq_len_list = self.dev_one_epoch(sess, test)
            self.evaluate(label_list, seq_len_list, test)

    def demo_one(self, sess, sent):
        """

        :param sess:
        :param sent:
        :return:
        """
        label_list = []
        for seqs, labels in batch_yield(sent, self.batch_size, self.vocab, self.tag2label, shuffle=False):
            label_list_, _ = self.predict_one_batch(sess, seqs)
            label_list.extend(label_list_)
        label2tag = {}
        for tag, label in self.tag2label.items():
            label2tag[label] = tag if label != 0 else label
        tag = [label2tag[label] for label in label_list[0]]
        return tag

    def run_one_epoch(self, sess, train, dev, tag2label, epoch, saver):
        """

        :param sess:
        :param train:
        :param dev:
        :param tag2label:
        :param epoch:
        :param saver:
        :return:
        """
        #len(train)为多少句话，batch_size为一次训练多少个样本，//表示整除，结果取整，往小了取
        num_batches = (len(train) + self.batch_size - 1) // self.batch_size
        start_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        batches = batch_yield(train, self.batch_size, self.vocab, self.tag2label, shuffle=self.shuffle)
        for step, (seqs, labels, vectors) in enumerate(batches):
            sys.stdout.write(' processing: {} batch / {} batches.'.format(step + 1, num_batches) + '\r')
            #epoch,step都是从0开始的
            step_num = epoch * num_batches + step + 1
            feed_dict, _ = self.get_feed_dict_v(seqs, labels, vectors, self.lr, self.dropout_keep_prob)
            _, loss_train, summary, step_num_ = sess.run([self.train_op, self.loss, self.merged, self.global_step],
                                                         feed_dict=feed_dict)

            if step + 1 == 1 or (step + 1) % 300 == 0 or step + 1 == num_batches:
                self.logger.info(
                    '{} epoch {}, step {}, loss: {:.4}, global_step: {}'.format(start_time, epoch + 1, step + 1,
                                                                                loss_train, step_num))

            self.file_writer.add_summary(summary, step_num)
            if step + 1 == num_batches:
                saver.save(sess, self.model_path, global_step=step_num)

        self.logger.info('===========validation / test===========')
        label_list_dev, seq_len_list_dev = self.dev_one_epoch(sess, dev)
        self.evaluate(label_list_dev, seq_len_list_dev, dev, epoch)


    def get_feed_dict_v(self, seqs, labels=None,vectors=None, lr=None, dropout=None):
        """
        :param seqs:
        :param labels:
        :param lr:
        :param dropout:
        :return: feed_dict
        """
        #pad_sequences返回的是填充后的句子列表，以及原来的句子长度
        # print('seqs:   ',seqs)
        word_ids, seq_len_list = pad_sequences(seqs, pad_mark=0)

        feed_dict = {self.word_ids: word_ids,
                     self.sequence_lengths: seq_len_list}

        if labels is not None:
            #0代表“o”，表示其他
            labels_, labels_len_list = pad_sequences(labels, pad_mark=0)
            feed_dict[self.labels] = labels_

        if vectors is not None:
            vectors_, vectors__len_list= pad_sequences(vectors, pad_mark=0)
            for indexi,ii in enumerate(vectors_):
                for indexj,jj in enumerate(ii):
                    # print(jj)
                    # print('jj:     ',len(jj))
                    if jj==[] or jj==0:
                    	vectors_[indexi][indexj]=[0]
                    	for kk in range(self.vectors_len-1):#100维的词向量
                        	vectors_[indexi][indexj].append(0)
                    # print('::: ',vectors_[indexi][indexj])
            feed_dict[self.vectors] = vectors_

        if lr is not None:
            feed_dict[self.lr_pl] = float(lr)

        if dropout is not None:
            feed_dict[self.dropout_pl] = dropout

        return feed_dict, seq_len_list


    def dev_one_epoch(self, sess, dev):
        """

        :param sess:
        :param dev:
        :return:
        """
        label_list, seq_len_list = [], []
        for seqs, labels, vectors in batch_yield(dev, self.batch_size, self.vocab, self.tag2label, shuffle=False):
            label_list_, seq_len_list_ = self.predict_one_batch(sess, seqs,vectors)
            label_list.extend(label_list_)
            seq_len_list.extend(seq_len_list_)
        return label_list, seq_len_list

    def predict_one_batch(self, sess, seqs, vectors):
        """

        :param sess:
        :param seqs:
        :return: label_list
                 seq_len_list
        """
        feed_dict, seq_len_list = self.get_feed_dict_v(seqs,vectors=vectors, dropout=1.0)

        if self.CRF:
            #计算损失与转移矩阵
            logits, transition_params = sess.run([self.logits, self.transition_params],
                                                 feed_dict=feed_dict)
            label_list = []
            for logit, seq_len in zip(logits, seq_len_list):
                # print(logit)
                # print('############################################')
                #print(seq_len)
                #将最有可能的序列求出来了,seq_len为原来未填充时的句子长度列表
                if seq_len==0:
                    continue
                viterbi_seq, _ = viterbi_decode(logit[:seq_len], transition_params)
                # print('&&&&&&&&&&&&&&&&&&&&&&%^^^^^^^^^^^^^^^^^^^^^')
                # print(viterbi_seq)

                label_list.append(viterbi_seq)
            return label_list, seq_len_list

        else:
            label_list = sess.run(self.labels_softmax_, feed_dict=feed_dict)
            return label_list, seq_len_list

    def evaluate(self, label_list, seq_len_list, data, epoch=None):
        """

        :param label_list:
        :param seq_len_list:
        :param data:
        :param epoch:
        :return:
        """
        label2tag = {}
        for tag, label in self.tag2label.items():
            #这里等价于：if label != 0：
            #                 label2tag[label] = tag
            #            else：
            #                 label2tag[label] = label
            label2tag[label] = tag if label != 0 else label
            
        #print('label2tag:     ',label2tag)
        
        model_predict = []
        for label_, (sent, tag,vectorsss) in zip(label_list, data):
            
            tag_ = [label2tag[label__] for label__ in label_]
            sent_res = []
            if  len(label_) != len(sent):
                print('label_:     ',label_)
                print(len(label_))
                print('sent:     ',sent)
                print(len(sent))
                print('tag:     ',tag)
                print(len(tag))
                print('tag_: ',tag_)
                print('tag_: ',len(tag_))
                continue
            for i in range(len(sent)):
                sent_res.append([sent[i], tag[i], tag_[i]])
                
            model_predict.append(sent_res)
        #以下等同于：if epoch!=None ：
        #                epoch_num = str(epoch+1)
        #            else：
        #                epoch_num="test"
        epoch_num = str(epoch+1) if epoch != None else 'test'
        label_path = os.path.join(self.result_path, 'label_' + epoch_num)
        metric_path = os.path.join(self.result_path, 'result_metric_' + epoch_num)
        for _ in conlleval(model_predict, label_path, metric_path):
            self.logger.info(_)

