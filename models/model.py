from __future__ import division, print_function, unicode_literals

import json
import math
import operator
import os
import random
from io import open
from queue import PriorityQueue # for py3
from functools import reduce # for py3

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import optim

import models.policy as policy

from utils.util import SOS_token, EOS_token, PAD_token, detected_device
default_device = detected_device
# SOS_token = 0
# EOS_token = 1
# UNK_token = 2
# PAD_token = 3
# use_moe_loss = True # inner models weighting loss
# learn_loss_weight = True
# use_moe_model = True # inner models structure partition
#

# Shawn beam search decoding
class BeamSearchNode(object):
    def __init__(self, h, prevNode, wordid, logp, leng):
        self.h = h
        self.prevNode = prevNode
        self.wordid = wordid
        self.logp = logp
        self.leng = leng

    def eval(self, repeatPenalty, tokenReward, scoreTable, alpha=1.0):
        reward = 0
        alpha = 1.0

        return self.logp / float(self.leng - 1 + 1e-6) + alpha * reward


def init_lstm(cell, gain=1):
    init_gru(cell, gain)

    # positive forget gate bias (Jozefowicz et al., 2015)
    for _, _, ih_b, hh_b in cell.all_weights:
        l = len(ih_b)
        ih_b[l // 4:l // 2].data.fill_(1.0)
        hh_b[l // 4:l // 2].data.fill_(1.0)


def init_gru(gru, gain=1):
    gru.reset_parameters()
    for _, hh, _, _ in gru.all_weights:
        for i in range(0, hh.size(0), gru.hidden_size):
            torch.nn.init.orthogonal_(hh[i:i+gru.hidden_size],gain=gain)


def whatCellType(input_size, hidden_size, cell_type, dropout_rate):
    if cell_type == 'rnn':
        cell = nn.RNN(input_size, hidden_size, dropout=dropout_rate, batch_first=False)
        init_gru(cell)
        return cell
    elif cell_type == 'gru':
        cell = nn.GRU(input_size, hidden_size, dropout=dropout_rate, batch_first=False)
        init_gru(cell)
        return cell
    elif cell_type == 'lstm':
        cell = nn.LSTM(input_size, hidden_size, dropout=dropout_rate, batch_first=False)
        init_lstm(cell)
        return cell
    elif cell_type == 'bigru':
        cell = nn.GRU(input_size, hidden_size, bidirectional=True, dropout=dropout_rate, batch_first=False)
        init_gru(cell)
        return cell
    elif cell_type == 'bilstm':
        cell = nn.LSTM(input_size, hidden_size, bidirectional=True, dropout=dropout_rate, batch_first=False)
        init_lstm(cell)
        return cell


class EncoderRNN(nn.Module):
    def __init__(self, input_size,  embedding_size, hidden_size, cell_type, depth, dropout, device=default_device):
        super(EncoderRNN, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.embed_size = embedding_size
        self.n_layers = depth
        self.dropout = dropout
        self.bidirectional = False
        if 'bi' in cell_type:
            self.bidirectional = True
        padding_idx = 3
        self.embedding = nn.Embedding(input_size, embedding_size, padding_idx=padding_idx)
        self.rnn = whatCellType(embedding_size, hidden_size,
                    cell_type, dropout_rate=self.dropout)
        self.device = device

    def forward(self, input_seqs, input_lens, hidden=None):
        """
        forward procedure. **No need for inputs to be sorted**
        :param input_seqs: Variable of [T,B]
        :param hidden:
        :param input_lens: *numpy array* of len for each input sequence
        :return:
        """
        input_lens = np.asarray(input_lens)
        input_seqs = input_seqs.transpose(0,1)
        #batch_size = input_seqs.size(1)
        embedded = self.embedding(input_seqs)
        embedded = embedded.transpose(0, 1)  # [B,T,E]
        sort_idx = np.argsort(-input_lens)
        # pp added
        unsort_idx = np.argsort(sort_idx)
        # unsort_idx = torch.LongTensor(np.argsort(sort_idx))
        input_lens = input_lens[sort_idx]
        # sort_idx = torch.LongTensor(sort_idx)
        embedded = embedded[sort_idx].transpose(0, 1)  # [T,B,E]
        packed = torch.nn.utils.rnn.pack_padded_sequence(embedded, input_lens)
        outputs, hidden = self.rnn(packed, hidden)
        outputs, _ = torch.nn.utils.rnn.pad_packed_sequence(outputs)
        if self.bidirectional:
            outputs = outputs[:, :, :self.hidden_size] + outputs[:, :, self.hidden_size:]

        outputs = outputs.transpose(0, 1)[unsort_idx].transpose(0, 1).contiguous()

        if isinstance(hidden, tuple):
            hidden = list(hidden)
            hidden[0] = hidden[0].transpose(0, 1)[unsort_idx].transpose(0, 1).contiguous()
            hidden[1] = hidden[1].transpose(0, 1)[unsort_idx].transpose(0, 1).contiguous()
            hidden = tuple(hidden)
        else:
            hidden = hidden.transpose(0, 1)[unsort_idx].transpose(0, 1).contiguous()

        return outputs, hidden


class Attn(nn.Module):
    def __init__(self, method, hidden_size, device=default_device):
        super(Attn, self).__init__()
        self.method = method
        self.hidden_size = hidden_size
        self.attn = nn.Linear(self.hidden_size * 2, hidden_size)
        self.v = nn.Parameter(torch.rand(hidden_size))
        stdv = 1. / math.sqrt(self.v.size(0))
        self.v.data.normal_(mean=0, std=stdv)
        self.device = device

    def forward(self, hidden, encoder_outputs):
        '''
        :param hidden:
            previous hidden state of the decoder, in shape (layers*directions,B,H)
        :param encoder_outputs:
            encoder outputs from Encoder, in shape (T,B,H)
        :return
            attention energies in shape (B,T)
        '''
        max_len = encoder_outputs.size(0)

        H = hidden.repeat(max_len,1,1).transpose(0,1)
        encoder_outputs = encoder_outputs.transpose(0,1)  # [T,B,H] -> [B,T,H]
        attn_energies = self.score(H,encoder_outputs)  # compute attention score
        return F.softmax(attn_energies, dim=1).unsqueeze(1)  # normalize with softmax

    def score(self, hidden, encoder_outputs):
        cat = torch.cat([hidden, encoder_outputs], 2)
        energy = torch.tanh(self.attn(cat)) # [B*T*2H]->[B*T*H]
        energy = energy.transpose(2,1) # [B*H*T]
        v = self.v.repeat(encoder_outputs.data.shape[0],1).unsqueeze(1) #[B*1*H]
        energy = torch.bmm(v,energy)  # [B*1*T]
        return energy.squeeze(1)  # [B*T]


class SeqAttnDecoderRNN(nn.Module):
    def __init__(self, embedding_size, hidden_size, output_size, cell_type, dropout_p=0.1, max_length=30, device=default_device):
        super(SeqAttnDecoderRNN, self).__init__()
        # Define parameters
        self.hidden_size = hidden_size
        self.embed_size = embedding_size
        self.output_size = output_size
        self.n_layers = 1
        self.dropout_p = dropout_p
        self.device = device

        # Define layers
        self.embedding = nn.Embedding(output_size, embedding_size)
        self.dropout = nn.Dropout(dropout_p)

        if 'bi' in cell_type:  # we dont need bidirectionality in decoding
            cell_type = cell_type.strip('bi')
        self.rnn = whatCellType(embedding_size + hidden_size, hidden_size, cell_type, dropout_rate=self.dropout_p)
        self.out = nn.Linear(hidden_size, output_size)

        self.score = nn.Linear(self.hidden_size + self.hidden_size, self.hidden_size)
        self.attn_combine = nn.Linear(embedding_size + hidden_size, embedding_size)

        # attention
        self.method = 'concat'
        self.attn = nn.Linear(self.hidden_size * 2, hidden_size)
        self.v = nn.Parameter(torch.rand(hidden_size))
        stdv = 1. / math.sqrt(self.v.size(0))
        self.v.data.normal_(mean=0, std=stdv)

    def forward(self, input, hidden, encoder_outputs, mask_tensor=None):
        if isinstance(hidden, tuple):
            h_t = hidden[0]
        else:
            h_t = hidden
        encoder_outputs = encoder_outputs.transpose(0, 1)
        embedded = self.embedding(input)  # .view(1, 1, -1)
        # embedded = F.dropout(embedded, self.dropout_p)

        # SCORE 3
        max_len = encoder_outputs.size(1)
        h_t = h_t.transpose(0, 1)  # [1,B,D] -> [B,1,D]
        h_t = h_t.repeat(1, max_len, 1)  # [B,1,D]  -> [B,T,D]
        energy = self.attn(torch.cat((h_t, encoder_outputs), 2))  # [B,T,2D] -> [B,T,D]
        energy = torch.tanh(energy)
        energy = energy.transpose(2, 1)  # [B,H,T]
        v = self.v.repeat(encoder_outputs.size(0), 1).unsqueeze(1)  # [B,1,H]
        energy = torch.bmm(v, energy)  # [B,1,T]
        attn_weights = F.softmax(energy, dim=2)  # [B,1,T]

        # getting context
        context = torch.bmm(attn_weights, encoder_outputs)  # [B,1,H]

        # context = torch.bmm(attn_weights.unsqueeze(0), encoder_outputs.unsqueeze(0)) #[B,1,H]
        # Combine embedded input word and attended context, run through RNN
        rnn_input = torch.cat((embedded, context), 2)
        rnn_input = rnn_input.transpose(0, 1)
        output, hidden = self.rnn(rnn_input, hidden)
        output = output.squeeze(0)  # (1,B,V)->(B,V)

        output = F.log_softmax(self.out(output), dim=1)
        return output, hidden  # , attn_weights

class MoESeqAttnDecoderRNN(nn.Module):
    def __init__(self, embedding_size, hidden_size, output_size, cell_type, k=1, dropout_p=0.1, max_length=30, args=None,  device=default_device):
        super(MoESeqAttnDecoderRNN, self).__init__()
        # Define parameters
        self.hidden_size = hidden_size
        self.embed_size = embedding_size
        self.output_size = output_size
        self.n_layers = 1
        self.dropout_p = dropout_p
        self.k = k
        self.device = device
        self.args = args

        # Define layers
        self.embedding = nn.Embedding(output_size, embedding_size)
        self.dropout = nn.Dropout(dropout_p)

        if 'bi' in cell_type:  # we dont need bidirectionality in decoding
            cell_type = cell_type.strip('bi')
        self.rnn = whatCellType(embedding_size + hidden_size, hidden_size, cell_type, dropout_rate=self.dropout_p)
        self.moe_rnn = whatCellType(hidden_size*(self.k+1), hidden_size*(self.k+1), cell_type, dropout_rate=self.dropout_p)
        self.moe_hidden = nn.Linear(hidden_size * (self.k+1), hidden_size)
        self.moe_fc = nn.Linear(output_size*(self.k+1), (self.k+1))
        self.out = nn.Linear(hidden_size, output_size)
        self.score = nn.Linear(self.hidden_size + self.hidden_size, self.hidden_size)
        self.attn_combine = nn.Linear(embedding_size + hidden_size, embedding_size)

        # attention
        self.method = 'concat'
        self.attn = nn.Linear(self.hidden_size * 2, hidden_size)
        self.v = nn.Parameter(torch.rand(hidden_size))
        stdv = 1. / math.sqrt(self.v.size(0))
        self.v.data.normal_(mean=0, std=stdv)

    def expert_forward(self, input, hidden, encoder_outputs):
        if isinstance(hidden, tuple):
            h_t = hidden[0]
        else:
            h_t = hidden
        encoder_outputs = encoder_outputs.transpose(0, 1)

        embedded = self.embedding(input)  # .view(1, 1, -1)
        # embedded = F.dropout(embedded, self.dropout_p)

        # SCORE 3
        max_len = encoder_outputs.size(1)
        h_t = h_t.transpose(0, 1)  # [1,B,D] -> [B,1,D]
        h_t = h_t.repeat(1, max_len, 1)  # [B,1,D]  -> [B,T,D]
        energy = self.attn(torch.cat((h_t, encoder_outputs), 2))  # [B,T,2D] -> [B,T,D]
        energy = torch.tanh(energy)
        energy = energy.transpose(2, 1)  # [B,H,T]
        v = self.v.repeat(encoder_outputs.size(0), 1).unsqueeze(1)  # [B,1,H]
        energy = torch.bmm(v, energy)  # [B,1,T]
        attn_weights = F.softmax(energy, dim=2)  # [B,1,T]

        # getting context
        context = torch.bmm(attn_weights, encoder_outputs)  # [B,1,H]

        # Combine embedded input word and attended context, run through RNN
        rnn_input = torch.cat((embedded, context), 2)
        rnn_input = rnn_input.transpose(0, 1)
        output, hidden = self.rnn(rnn_input, hidden)
        output = output.squeeze(0)  # (1,B,H)->(B,H)

        output = F.log_softmax(self.out(output), dim=1) # self.out(output)[batch, out_vocab]
        return output, hidden, embedded.transpose(0, 1)  # , attn_weights

    def moe_layer(self, decoder_output_list, decoder_hidden_list, embedded_list, gamma_expert):
        # output
        chair_dec_out = decoder_output_list[0] # chair
        expert_dec_out_list = decoder_output_list[1:] # experts
        chair_dec_hid = decoder_hidden_list[0] # chair
        expert_dec_hid_list = decoder_hidden_list[1:] # experts
        cat_dec_out = torch.cat(decoder_output_list, -1) # (B, (k+1)*V) # Experts
        # MOE weights computation + normalization ------ Start
        moe_weights = self.moe_fc(cat_dec_out) #[Batch, Intent]
        moe_weights = F.log_softmax(moe_weights, dim=1)
        # moe_weights = F.softmax(moe_weights, dim=1)

        # available_m = torch.zeros(moe_weights.size(), device=self.device)
        # i = 0
        # for k in enumerate(decoder_output_list):
        #     available_m[:,i] = mask_tensor[k]
        #     i += 1
        # moe_weights = available_m * moe_weights

        norm_weights = torch.sum(moe_weights, dim=1)
        norm_weights = norm_weights.unsqueeze(1)
        moe_weights = torch.div(moe_weights, norm_weights) # [B, I]
        moe_weights = moe_weights.permute(1,0).unsqueeze(-1) # [I, B, 1]; debug:[8,2,1]
        # MOE weights computation + normalization ------ End
        # output
        moe_weights_output = moe_weights.expand(-1, -1, decoder_output_list[0].size(-1))  # [I, B, V]; [8,2,400]
        decoder_output_tensor = torch.stack(decoder_output_list) # [I, B, V]
        output = decoder_output_tensor.mul(moe_weights_output).sum(0)  # [B, V]; [2, 400]
        # weighting
        output = gamma_expert * output + (1-gamma_expert) * chair_dec_out # [2, 400]
        # hidden
        moe_weights_hidden = moe_weights.expand(-1, -1, decoder_hidden_list[0][0].size(-1))  # [I, B, H]; [8,2,5]
        stack_dec_hid = torch.stack([a.squeeze(0) for a, b in decoder_hidden_list]), torch.stack([b.squeeze(0) for a, b in decoder_hidden_list]) # [I, B, H]
        hidden = stack_dec_hid[0].mul(moe_weights_hidden).sum(0).unsqueeze(0), stack_dec_hid[1].mul(moe_weights_hidden).sum(0).unsqueeze(0) # [B, H]
        hidden = gamma_expert * hidden[0] + (1-gamma_expert) * chair_dec_hid[0], gamma_expert * hidden[1] + (1-gamma_expert) * chair_dec_hid[1]
        return output, hidden # output[B, V] -- [2, 400] ; hidden[1, B, H] -- [1, 2, 5]

    def tokenMoE(self, decoder_input, decoder_hidden, encoder_outputs, mask_tensor):
        # decoder_input[batch, 1]; decoder_hidden: tuple element is a tensor[1, batch, hidden], encoder_outputs[maxlen_target, batch, hidden]
        # n = len(self.intent_list) # how many intents do we have
        output_c, hidden_c, embedded_c = self.expert_forward(input=decoder_input, hidden=decoder_hidden,
                                                             encoder_outputs=encoder_outputs)
        decoder_output_list, decoder_hidden_list, embedded_list = [output_c], [hidden_c], [embedded_c]
        # decoder_output_list, decoder_hidden_list, embedded_list = [], [], []
        # count = 0
        for mask in mask_tensor: # each intent has a mask [Batch, 1]
            decoder_input_k = decoder_input.clone().masked_fill_(mask, value=PAD_token) # if assigned PAD_token it will count loss
            decoder_hidden_k = tuple(map(lambda x: x.clone().masked_fill_(mask, value=PAD_token), decoder_hidden))
            encoder_outputs_k = encoder_outputs.clone().masked_fill_(mask, value=PAD_token)
            # test if there's someone not all PADDED
            # if torch.min(decoder_input_k)!=PAD_token or torch.min(decoder_hidden_k[0])!=PAD_token or torch.min(decoder_hidden_k[1])!=PAD_token or torch.min(encoder_outputs_k)!=PAD_token:
                # print(decoder_input_k, '\n', decoder_hidden_k,'\n', encoder_outputs_k)
                # count += 1
            output_k, hidden_k, embedded_k = self.expert_forward(input=decoder_input_k, hidden=decoder_hidden_k, encoder_outputs=encoder_outputs_k)

            decoder_output_list.append(output_k)
            decoder_hidden_list.append(hidden_k)
            embedded_list.append(embedded_k)

        # print('count=', count) # 10/31 will count for loss
        gamma_expert = self.args.gamma_expert
        decoder_output, decoder_hidden = self.moe_layer(decoder_output_list, decoder_hidden_list, embedded_list, gamma_expert)
        # decoder_output = gamma_expert * decoder_output + (1 - gamma_expert) * output_c
        # decoder_hidden = gamma_expert * decoder_hidden + (1 - gamma_expert) * hidden_c
        # output = output.squeeze(0)  # (1,B,H)->(B,H)
        # output = F.log_softmax(self.out(output), dim=1) # self.out(output)[batch, out_vocab]
        return decoder_output, decoder_hidden

    def forward(self, input, hidden, encoder_outputs, mask_tensor):
        if mask_tensor is not None:
            output, hidden = self.tokenMoE(input, hidden, encoder_outputs, mask_tensor)
        else:
            pass
            output, hidden, _ = self.expert_forward(input, hidden, encoder_outputs)
        return output, hidden #, mask_tensor  # , attn_weights

class DecoderRNN(nn.Module):
    def __init__(self, embedding_size, hidden_size, output_size, cell_type, dropout=0.1, device=default_device):
        super(DecoderRNN, self).__init__()
        self.device = device
        self.hidden_size = hidden_size
        self.cell_type = cell_type
        padding_idx = 3
        self.embedding = nn.Embedding(num_embeddings=output_size,
                                      embedding_dim=embedding_size,
                                      padding_idx=padding_idx
                                      )
        if 'bi' in cell_type:  # we dont need bidirectionality in decoding
            cell_type = cell_type.strip('bi')
        self.rnn = whatCellType(embedding_size, hidden_size, cell_type, dropout_rate=dropout)
        self.dropout_rate = dropout
        self.out = nn.Linear(hidden_size, output_size)

    def forward(self, input, hidden, not_used, mask_tensor=None):
        embedded = self.embedding(input).transpose(0, 1)  # [B,1] -> [ 1,B, D]
        embedded = F.dropout(embedded, self.dropout_rate)

        output = embedded
        #output = F.relu(embedded)

        output, hidden = self.rnn(output, hidden)

        out = self.out(output.squeeze(0))
        output = F.log_softmax(out, dim=1)

        return output, hidden


class Model(nn.Module):
    def __init__(self, args, input_lang_index2word, output_lang_index2word, input_lang_word2index, output_lang_word2index, intent2index=None, index2intent=None, device=default_device):
        super(Model, self).__init__()
        self.args = args
        self.max_len = args.max_len

        self.output_lang_index2word = output_lang_index2word
        self.input_lang_index2word = input_lang_index2word

        self.output_lang_word2index = output_lang_word2index
        self.input_lang_word2index = input_lang_word2index

        # pp added
        self.intent2index, self.index2intent = intent2index, index2intent
        self.k = len(self.intent2index) if self.intent2index else 1

        self.hid_size_enc = args.hid_size_enc
        self.hid_size_dec = args.hid_size_dec
        self.hid_size_pol = args.hid_size_pol

        self.emb_size = args.emb_size
        self.db_size = args.db_size
        self.bs_size = args.bs_size
        self.cell_type = args.cell_type
        if 'bi' in self.cell_type:
            self.num_directions = 2
        else:
            self.num_directions = 1
        self.depth = args.depth
        self.use_attn = args.use_attn
        self.attn_type = args.attention_type

        self.dropout = args.dropout
        self.device = device

        self.model_dir = args.model_dir
        self.model_name = args.model_name
        self.teacher_forcing_ratio = args.teacher_ratio
        self.vocab_size = args.vocab_size
        self.epsln = 10E-5


        torch.manual_seed(args.seed)
        self.build_model()
        self.getCount()
        try:
            assert self.args.beam_width > 0
            self.beam_search = True
        except:
            self.beam_search = False

        self.global_step = 0

    def cuda_(self, var):
        return var.cuda() if self.args.cuda else var

    def build_model(self):
        self.encoder = EncoderRNN(len(self.input_lang_index2word), self.emb_size, self.hid_size_enc,
                                  self.cell_type, self.depth, self.dropout)

        self.policy = policy.DefaultPolicy(self.hid_size_pol, self.hid_size_enc, self.db_size, self.bs_size)

        # pp added: intent_type branch
        if self.args.intent_type and self.args.use_moe_model:
            self.decoder = MoESeqAttnDecoderRNN(self.emb_size, self.hid_size_dec, len(self.output_lang_index2word), self.cell_type, self.k, self.dropout, self.max_len, self.args)
        elif self.use_attn:
            if self.attn_type == 'bahdanau':
                self.decoder = SeqAttnDecoderRNN(self.emb_size, self.hid_size_dec, len(self.output_lang_index2word), self.cell_type, self.dropout, self.max_len)
        else:
            self.decoder = DecoderRNN(self.emb_size, self.hid_size_dec, len(self.output_lang_index2word), self.cell_type, self.dropout)

        if self.args.mode == 'train':
            self.gen_criterion = nn.NLLLoss(ignore_index=PAD_token, reduction='mean')  # logsoftmax is done in decoder part
            self.setOptimizers()

        # pp added
        self.moe_loss_layer = nn.Linear(1 * (self.k + 1), 1)

    def model_train(self, input_tensor, input_lengths, target_tensor, target_lengths, db_tensor, bs_tensor, mask_tensor=None, dial_name=None):

        proba, _, decoded_sent = self.forward(input_tensor, input_lengths, target_tensor, target_lengths, db_tensor, bs_tensor, mask_tensor) # pp added: acts_list

        proba = proba.view(-1, self.vocab_size)

        self.gen_loss = self.gen_criterion(proba, target_tensor.view(-1))

        if self.args.use_moe_loss and mask_tensor is not None:  # data separate by intents:
            gen_loss_list = []
            for mask in mask_tensor:  # each intent has a mask [Batch, 1]
                target_tensor_i = target_tensor.clone()
                target_tensor_i = target_tensor_i.masked_fill_(mask, value=PAD_token)
                loss_i = self.gen_criterion(proba, target_tensor_i.view(-1))
                gen_loss_list.append(loss_i)

            if self.args.learn_loss_weight:
                gen_loss_list.append(self.gen_loss)
                gen_loss_tensor = torch.as_tensor(torch.stack(gen_loss_list), device=self.device)
                self.gen_loss = self.moe_loss_layer(gen_loss_tensor)
            else: # hyper weights
                # lambda_expert = 0.5
                lambda_expert = self.args.lambda_expert
                self.gen_loss = lambda_expert * self.gen_loss + (1-lambda_expert) * torch.mean(torch.tensor(gen_loss_list))
        self.loss = self.gen_loss
        self.loss.backward()
        grad = self.clipGradients()
        self.optimizer.step()
        self.optimizer.zero_grad()

        #self.printGrad()
        return self.loss.item(), 0, grad

    def setOptimizers(self):
        self.optimizer_policy = None
        if self.args.optim == 'sgd':
            self.optimizer = optim.SGD(lr=self.args.lr_rate, params=filter(lambda x: x.requires_grad, self.parameters()), weight_decay=self.args.l2_norm)
        elif self.args.optim == 'adadelta':
            self.optimizer = optim.Adadelta(lr=self.args.lr_rate, params=filter(lambda x: x.requires_grad, self.parameters()), weight_decay=self.args.l2_norm)
        elif self.args.optim == 'adam':
            self.optimizer = optim.Adam(lr=self.args.lr_rate, params=filter(lambda x: x.requires_grad, self.parameters()), weight_decay=self.args.l2_norm)

    def forward(self, input_tensor, input_lengths, target_tensor, target_lengths, db_tensor, bs_tensor, mask_tensor=None): # pp added: acts_list
        """Given the user sentence, user belief state and database pointer,
        encode the sentence, decide what policy vector construct and
        feed it as the first hiddent state to the decoder.
        input_tensor: tensor(batch, maxlen_input)
        target_tensor: tensor(batch, maxlen_target)
        """

        target_length = target_tensor.size(1)

        # for fixed encoding this is zero so it does not contribute
        batch_size, seq_len = input_tensor.size()

        # ENCODER
        encoder_outputs, encoder_hidden = self.encoder(input_tensor, input_lengths) # encoder_outputs: tensor(maxlen_input, batch, 150); encoder_hidden: tuple, each element is a tensor: [1, batch, 150]

        # POLICY
        decoder_hidden = self.policy(encoder_hidden, db_tensor, bs_tensor) # decoder_hidden: tuple, each element is a tensor: [1, batch, 150]

        # GENERATOR
        # Teacher forcing: Feed the target as the next input
        _, target_len = target_tensor.size()

        decoder_input = torch.as_tensor([[SOS_token] for _ in range(batch_size)], dtype=torch.long, device=self.device) # tensor[batch, 1]
        # decoder_input = torch.LongTensor([[SOS_token] for _ in range(batch_size)], device=self.device)

        # pp added: calculate new batch size
        proba = torch.zeros(batch_size, target_length, self.vocab_size, device=self.device)  # tensor[Batch, maxlen_target, V]

        # generate target sequence step by step !!!
        for t in range(target_len):
            # pp added: moe chair
            decoder_output, decoder_hidden = self.decoder(decoder_input, decoder_hidden, encoder_outputs, mask_tensor) # decoder_output; decoder_hidden

            use_teacher_forcing = True if random.random() < self.args.teacher_ratio else False
            if use_teacher_forcing:
                decoder_input = target_tensor[:, t].view(-1, 1)  # [B,1] Teacher forcing
            else:
                # Without teacher forcing: use its own predictions as the next input
                topv, topi = decoder_output.topk(1)
                decoder_input = topi.squeeze().detach()  # detach from history as input

            proba[:, t, :] = decoder_output # decoder_output[Batch, TargetVocab]

        # if we consider sentence info
        if self.args.SentMoE:
           # generate target sequence step by step !!!
           for t in range(target_len):
               # pp added: moe chair
               decoder_output, decoder_hidden = self.decoder(decoder_input, decoder_hidden, encoder_outputs,
                                                             mask_tensor)  # decoder_output; decoder_hidden

               use_teacher_forcing = True if random.random() < self.args.teacher_ratio else False
               if use_teacher_forcing:
                   decoder_input = target_tensor[:, t].view(-1, 1)  # [B,1] Teacher forcing
               else:
                   # Without teacher forcing: use its own predictions as the next input
                   topv, topi = decoder_output.topk(1)
                   decoder_input = topi.squeeze().detach()  # detach from history as input

               proba[:, t, :] = decoder_output  # decoder_output[Batch, TargetVocab]


        decoded_sent = None

        return proba, None, decoded_sent

    def predict(self, input_tensor, input_lengths, target_tensor, target_lengths, db_tensor, bs_tensor, mask_tensor=None):
        # pp added
        with torch.no_grad():
            # ENCODER
            encoder_outputs, encoder_hidden = self.encoder(input_tensor, input_lengths)

            # POLICY
            decoder_hidden = self.policy(encoder_hidden, db_tensor, bs_tensor)

            # GENERATION
            decoded_words = self.decode(target_tensor, decoder_hidden, encoder_outputs, mask_tensor)

        return decoded_words, 0

    def decode(self, target_tensor, decoder_hidden, encoder_outputs, mask_tensor=None):
        decoder_hiddens = decoder_hidden

        if self.beam_search:  # wenqiang style - sequicity
            decoded_sentences = []
            for idx in range(target_tensor.size(0)): # idx is the batch index

                if isinstance(decoder_hiddens, tuple):  # LSTM case
                    decoder_hidden = (decoder_hiddens[0][:,idx, :].unsqueeze(0),decoder_hiddens[1][:,idx, :].unsqueeze(0))
                else:
                    decoder_hidden = decoder_hiddens[:, idx, :].unsqueeze(0)
                encoder_output = encoder_outputs[:,idx, :].unsqueeze(1)

                # Beam start
                self.topk = 1
                endnodes = []  # stored end nodes
                number_required = min((self.topk + 1), self.topk - len(endnodes))
                decoder_input = torch.as_tensor([[SOS_token]], dtype=torch.long, device=self.device)
                # decoder_input = torch.LongTensor([[SOS_token]], device=self.device)

                # starting node hidden vector, prevNode, wordid, logp, leng,
                node = BeamSearchNode(decoder_hidden, None, decoder_input, 0, 1)
                nodes = PriorityQueue()  # start the queue
                nodes.put((-node.eval(None, None, None, None),
                           node))

                # start beam search
                qsize = 1
                while True:
                    # give up when decoding takes too long
                    if qsize > 2000: break

                    # fetch the best node
                    score, n = nodes.get()
                    decoder_input = n.wordid
                    decoder_hidden = n.h

                    if n.wordid.item() == EOS_token and n.prevNode != None:  # its not empty
                        endnodes.append((score, n))
                        # if reach maximum # of sentences required
                        if len(endnodes) >= number_required:
                            break
                        else:
                            continue

                    # decode for one step using decoder
                    # import pdb
                    # pdb.set_trace()
                    mask_tensor_idx = mask_tensor[:, idx, :].unsqueeze(1) if mask_tensor is not None else None
                    decoder_output, decoder_hidden = self.decoder(decoder_input, decoder_hidden, encoder_output, mask_tensor_idx)

                    log_prob, indexes = torch.topk(decoder_output, self.args.beam_width)
                    nextnodes = []

                    for new_k in range(self.args.beam_width):
                        decoded_t = indexes[0][new_k].view(1, -1)
                        log_p = log_prob[0][new_k].item()

                        node = BeamSearchNode(decoder_hidden, n, decoded_t, n.logp + log_p, n.leng + 1)
                        score = -node.eval(None, None, None, None)
                        nextnodes.append((score, node))

                    # put them into queue
                    for i in range(len(nextnodes)):
                        score, nn = nextnodes[i]
                        nodes.put((score, nn))

                    # increase qsize
                    qsize += len(nextnodes)

                # choose nbest paths, back trace them
                if len(endnodes) == 0:
                    endnodes = [nodes.get() for n in range(self.topk)]

                utterances = []
                for score, n in sorted(endnodes, key=operator.itemgetter(0)):
                    utterance = []
                    utterance.append(n.wordid)
                    # back trace
                    while n.prevNode != None:
                        n = n.prevNode
                        utterance.append(n.wordid)

                    utterance = utterance[::-1]
                    utterances.append(utterance)

                decoded_words = utterances[0]
                decoded_sentence = [self.output_index2word(str(ind.item())) for ind in decoded_words]
                #print(decoded_sentence)
                decoded_sentences.append(' '.join(decoded_sentence[1:-1]))

            return decoded_sentences

        else:  # GREEDY DECODING
            # decoded_sentences = []
            decoded_sentences = self.greedy_decode(decoder_hidden, encoder_outputs, target_tensor, mask_tensor)
            return decoded_sentences

    def greedy_decode(self, decoder_hidden, encoder_outputs, target_tensor, mask_tensor=None):
        decoded_sentences = []
        batch_size, seq_len = target_tensor.size()
        # pp added
        decoder_input = torch.as_tensor([[SOS_token] for _ in range(batch_size)], dtype=torch.long, device=self.device)
        # decoder_input = torch.LongTensor([[SOS_token] for _ in range(batch_size)], device=self.device)

        decoded_words = torch.zeros((batch_size, self.max_len), device=self.device)
        for t in range(self.max_len):
            decoder_output, decoder_hidden = self.decoder(decoder_input, decoder_hidden, encoder_outputs, mask_tensor)

            topv, topi = decoder_output.data.topk(1)  # get candidates
            topi = topi.view(-1)

            decoded_words[:, t] = topi
            decoder_input = topi.detach().view(-1, 1)

        for sentence in decoded_words:
            sent = []
            for ind in sentence:
                if self.output_index2word(str(int(ind.item()))) == self.output_index2word(str(EOS_token)):
                    break
                sent.append(self.output_index2word(str(int(ind.item()))))
            decoded_sentences.append(' '.join(sent))

        return decoded_sentences

    def clipGradients(self):
        grad = torch.nn.utils.clip_grad_norm_(self.parameters(), self.args.clip)
        return grad

    def saveModel(self, iter):
        print('Saving parameters..')
        if not os.path.exists(self.model_dir):
            os.makedirs(self.model_dir)

        torch.save(self.encoder.state_dict(), self.model_dir + self.model_name + '-' + str(iter) + '.enc')
        torch.save(self.policy.state_dict(), self.model_dir + self.model_name + '-' + str(iter) + '.pol')
        torch.save(self.decoder.state_dict(), self.model_dir + self.model_name + '-' + str(iter) + '.dec')

        with open(self.model_dir + self.model_name + '.config', 'w') as f:
            f.write(json.dumps(vars(self.args), ensure_ascii=False, indent=4))

    def loadModel(self, iter=0):
        print('Loading parameters of iter %s ' % iter)
        self.encoder.load_state_dict(torch.load(self.model_dir + self.model_name + '-' + str(iter) + '.enc'))
        self.policy.load_state_dict(torch.load(self.model_dir + self.model_name + '-' + str(iter) + '.pol'))
        self.decoder.load_state_dict(torch.load(self.model_dir + self.model_name + '-' + str(iter) + '.dec'))

    def input_index2word(self, index):
        if index in self.input_lang_index2word:
            return self.input_lang_index2word[index]
        else:
            raise UserWarning('We are using UNK')

    def output_index2word(self, index):
        if index in self.output_lang_index2word:
            return self.output_lang_index2word[index]
        else:
            raise UserWarning('We are using UNK')

    def input_word2index(self, index):
        if index in self.input_lang_word2index:
            return self.input_lang_word2index[index]
        else:
            return 2

    def output_word2index(self, index):
        if index in self.output_lang_word2index:
            return self.output_lang_word2index[index]
        else:
            return 2
    # pp added:
    def input_intent2index(self, intent):
        if intent in self.intent2index:
            return self.intent2index[intent]
        else:
            return 0

    def input_index2intent(self, index):
        if index in self.index2intent:
            return self.index2intent[index]
        else:
            raise UserWarning('We are using UNK intent')

    def getCount(self):
        learnable_parameters = filter(lambda p: p.requires_grad, self.parameters())
        param_cnt = sum([reduce((lambda x, y: x * y), param.shape) for param in learnable_parameters])
        print('Model has', param_cnt, ' parameters.')

    def printGrad(self):
        learnable_parameters = filter(lambda p: p.requires_grad, self.parameters())
        for idx, param in enumerate(learnable_parameters):
            print(param.grad, param.shape)