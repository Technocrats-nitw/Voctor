
import tensorflow as tf
from tensorflow import keras
import tensorflow.keras.backend as K
from tf_bert.position_wise_feed_forward import FeedForward
from tf_bert.layer_normalization import LayerNormalization
from tf_bert.pos_embd import PositionEmbedding
from tf_bert.multi_head import MultiHeadAttention
from tf_bert.layers import get_inputs, get_embedding, TokenEmbedding, EmbeddingSimilarity, Masked, Extract


import json

'''
Implementation of BERT. Inherits tf_bert module and generates bert from the configuration file
    Here config file should be a json with following entries
      vocab_size = number of tokens reqd.
      max_position_embeddings = length of sequence
      hidden_size = for size of embedding layer (which is a hidden layer)
      num_hidden_layers = total number of hidden layers
      intermediate_size = dimension of feed_forward embedding that is to dimensions of vector matrix to transform bert embeddings to input to fcnn
      num_attention_head = transformer is using multi head attention so the num of required heads
      training = flag to specify training mode if false then backprop doesn't take place
      trainable = if false then weights will be freezed
'''
#activation function
def gelu(x):
    return 0.5 * x * (1.0 + tf.math.erf(x / tf.sqrt(2.0)))

#bert implementation
class Bert(keras.Model):
    def __init__(self,token_num,pos_num=512,seq_len=512,embed_dim=768,transformer_num=12,head_num=12,feed_forward_dim=3072,dropout_rate=0.1,
            attention_activation=None,feed_forward_activation=gelu,custom_layers=None,training=True,trainable=None,lr=1e-4,name='Bert'):
        super().__init__(name=name)
        self.token_num = token_num
        self.pos_num = pos_num
        self.seq_len = seq_len
        self.embed_dim = embed_dim
        self.transformer_num = transformer_num
        self.head_num = head_num
        self.feed_forward_dim = feed_forward_dim
        self.dropout_rate = dropout_rate
        self.attention_activation = attention_activation
        self.feed_forward_activation = feed_forward_activation
        self.custom_layers = custom_layers
        self.training = training
        self.trainable = trainable
        self.lr = lr

        # build layers
        # embedding
        self.position_embedding_layer = PositionEmbedding(
            input_dim=pos_num,
            output_dim=embed_dim,
            mode=PositionEmbedding.MODE_ADD,
            trainable=trainable,
            name='Embedding-Position',
        )
        self.segment_embedding_layer = keras.layers.Embedding(
            input_dim=2,
            output_dim=embed_dim,
            trainable=trainable,
            name='Embedding-Segment',
        )
        

        self.token_embedding_layer = TokenEmbedding(
            input_dim=token_num,
            output_dim=embed_dim,
            mask_zero=True,
            trainable=trainable,
            name='Embedding-Token',
        )
        
        self.embedding_layer_norm = LayerNormalization(
            trainable=trainable,
            name='Embedding-Norm',
        )
        self.encoder_attention_norm = []
        self.encoder_ffn_norm = []
        self.encoder_multihead_layers = []
        self.encoder_ffn_layers = []
        
        # attention layers
        for i in range(transformer_num):
            base_name = 'Encoder-%d' % (i + 1)
            attention_name = '%s-MultiHeadSelfAttention' % base_name
            feed_forward_name = '%s-FeedForward' % base_name
            self.encoder_multihead_layers.append(MultiHeadAttention(
                head_num=head_num,
                activation=attention_activation,
                history_only=False,
                trainable=trainable,
                name=attention_name,
            ))
            self.encoder_ffn_layers.append(FeedForward(
                units=feed_forward_dim,
                activation=feed_forward_activation,
                trainable=trainable,
                name=feed_forward_name,
            ))
            self.encoder_attention_norm.append(LayerNormalization(
                trainable=trainable,
                name='%s-Norm' % attention_name,
            ))
            self.encoder_ffn_norm.append(LayerNormalization(
                trainable=trainable,
                name='%s-Norm' % feed_forward_name,
            ))

    def call(self, inputs):

        embeddings = [
            self.token_embedding_layer(inputs[0]),
            self.segment_embedding_layer(inputs[1])
        ]
        embeddings[0], embed_weights = embeddings[0]
        embed_layer = keras.layers.Add(
            name='Embedding-Token-Segment')(embeddings)
        embed_layer = self.position_embedding_layer(embed_layer)

        if self.dropout_rate > 0.0:
            dropout_layer = keras.layers.Dropout(
                rate=self.dropout_rate,
                name='Embedding-Dropout',
            )(embed_layer)
        else:
            dropout_layer = embed_layer

        embedding_output = self.embedding_layer_norm(dropout_layer)
        # Wrap layers with residual, normalization and dropout.
        
        def _wrap_layer(name, input_layer, build_func, norm_layer, dropout_rate=0.0, trainable=True):
            # param build_func: A callable that takes the input tensor and generates the output tensor.
            build_output = build_func(input_layer)
            if dropout_rate > 0.0:
                dropout_layer = keras.layers.Dropout(
                    rate=dropout_rate,
                    name='%s-Dropout' % name,
                )(build_output)
            else:
                dropout_layer = build_output
            if isinstance(input_layer, list):
                input_layer = input_layer[0]
            add_layer = keras.layers.Add(name='%s-Add' %
                                         name)([input_layer, dropout_layer])
            normal_layer = norm_layer(add_layer)
            return normal_layer

        last_layer = embedding_output
        output_tensor_list = [last_layer]
        # self attention
        for i in range(self.transformer_num):
            base_name = 'Encoder-%d' % (i + 1)
            attention_name = '%s-MultiHeadSelfAttention' % base_name
            feed_forward_name = '%s-FeedForward' % base_name
            
            self_attention_output = _wrap_layer(
                name=attention_name,
                input_layer=last_layer,
                build_func=self.encoder_multihead_layers[i],
                norm_layer=self.encoder_attention_norm[i],
                dropout_rate=self.dropout_rate,
                trainable=self.trainable)
            
            last_layer = _wrap_layer(
                name=attention_name,
                input_layer=self_attention_output,
                build_func=self.encoder_ffn_layers[i],
                norm_layer=self.encoder_ffn_norm[i],
                dropout_rate=self.dropout_rate,
                trainable=self.trainable)
            output_tensor_list.append(last_layer)

        return output_tensor_list


def build_model_from_config(config_file,training=False,trainable=None,seq_len=None,build=True):
    
    # config_file: The path to the JSON configuration file.
    with open(config_file, 'r') as reader:
        config = json.loads(reader.read())
    # If it is not None and it is shorter than the value in the config file, the weights in position embeddings will be sliced to fit the new length.
    if seq_len is not None:
        config['max_position_embeddings'] = min(
            seq_len, config['max_position_embeddings'])
        
    # training: If training, the whole model will be returned.
    if trainable is None:
        trainable = training
    model = Bert(
        token_num=config['vocab_size'],
        pos_num=config['max_position_embeddings'],
        seq_len=config['max_position_embeddings'],
        embed_dim=config['hidden_size'],
        transformer_num=config['num_hidden_layers'],
        head_num=config['num_attention_heads'],
        feed_forward_dim=config['intermediate_size'],
        training=training,
        trainable=trainable,
    )
    if build:
        model.build(input_shape=[(None, None), (None, None), (None, None)])
    return model, config #returning model and config
