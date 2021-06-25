import argparse
import ast
import csv
import os
import torch
import random
import logging
from numpy import *
import numpy as np
import torch.nn.functional as F
from tokenizers import BertWordPieceTokenizer
from sklearn.metrics import accuracy_score,precision_score,recall_score,f1_score
from torch.utils.data import TensorDataset, DataLoader
from transformers import BertConfig, AdamW, get_linear_schedule_with_warmup
from model import MrBERT

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logging = logging.getLogger(__name__)

"""
2. Data pre-processing
"""
def load_mohx():
    """ 读取 MOH-X 数据
    """
    svo_labels ,seq_labels = [], []
    with open('./data/MOH-X/mohx_labels.csv', encoding='utf8') as f:
        lines = csv.reader(f)
        next(lines)
        for line in lines:
            svo_labels.append(ast.literal_eval(line[0]))
    with open('./data/embeddings_mohx/mohx_embeddings_ave.csv', encoding='utf8') as f:
        lines = csv.reader(f)
        next(lines)
        embeddings = []
        for line in lines:
            embeddings.append(ast.literal_eval(line[1]))
    raw_mohX = []
    with open('./data/MOH-X/MOH-X_formatted_svo_cleaned.csv', encoding='utf8') as f:
        lines = csv.reader(f)
        next(lines)
        i = 0
        for line in lines:
            sen = line[3].split()
            v_pos = int(line[4])
            s_pos = -1
            o_pos = -1
            if 1 in svo_labels[i]:
                s_pos = svo_labels[i].index(1)
            if 3 in svo_labels[i]:
                o_pos = svo_labels[i].index(3)
            label_seq = [0] * len(sen)
            label_seq[v_pos] = int(line[5])
            assert (len(label_seq) == len(sen))
            assert len(svo_labels[i]) == len(sen)
            raw_mohX.append([line[3].strip(), label_seq, s_pos, v_pos, o_pos, int(line[5]), embeddings[i]])
            i += 1
    random.shuffle(raw_mohX)
    return raw_mohX


def insert_tag(sentences, s_pos, v_pos, o_pos):
    '''
    准确插入！可能有多个subj/verb/obj
    :param sentences:
    :param s_pos:
    :param v_pos:
    :param o_pos:
    :return:
    '''
    tokenized_texts=[]
    for i in range(len(sentences)):
        sen = sentences[i].split()
        v = sen[v_pos[i]]
        sen[v_pos[i]] = '[verb] '+ v + ' [/verb]'
        if not (s_pos[i] == -1) and not (o_pos[i] == -1):
            s = sen[s_pos[i]]
            sen[s_pos[i]] = '[subj] ' + s + ' [/subj]'
            o = sen[o_pos[i]]
            sen[o_pos[i]] = '[obj] ' + o + ' [/obj]'
        elif not s_pos[i]==-1 and o_pos[i]==-1:
            s = sen[s_pos[i]]
            sen[s_pos[i]] = '[subj] ' + s + ' [/subj]'
        elif s_pos[i]==-1 and not o_pos[i]==-1:
            o = sen[o_pos[i]]
            sen[o_pos[i]] = '[obj] ' + o + ' [/obj]'
        txt = (' '.join(sen)).split()
        tokenized_texts.append(txt)
    return tokenized_texts


def get_inputs(tokenizer, texts, labels0, labels2, max_len, embeddings):
    '''
    对输入进行 encode 和长度固定（截长补短）
    '''
    ids = []
    labels = []
    i=0
    for txt in texts:
        id=[101]
        label=[-100]
        j=0
        for w in txt:
            enc = tokenizer.encode(w)
            id_w = enc.ids
            id.extend(id_w[1:len(id_w) - 1])
            if w =='[subj]' or w == '[/subj]' or w == '[verb]' or w == '[/verb]' or w =='[obj]' or w == '[/obj]':
                l=[-100]
            else:
                l = [labels0[i][j]]
                if len(enc.tokens)>3:
                    for t in range(2,len(id_w)-1):
                        l.append(-100)
                j += 1
            label.extend(l)
        id.append(102)
        label.append(-100)
        assert len(labels0[i]) == (len(label) - label.count(-100))
        assert len(label)==len(id)
        id = id + [0] * (max_len-len(id))
        label = label + [-100] * (max_len-len(label))
        ids.append(id)
        labels.append(label)
        i+=1
    input_ids = torch.tensor([[i for i in id] for id in ids])
    labels = torch.tensor([[i for i in label] for label in labels])
    labels2 = torch.tensor(labels2, dtype=torch.float)
    embeddings = torch.tensor(embeddings, dtype=torch.float)
    # ! 设置 mask_attention
    masks = torch.tensor([[float(i > 0) for i in input_id]
                             for input_id in input_ids])
    return input_ids, labels, labels2, masks, embeddings


def evaluation(labels2,preds2):

    accuracy2 = accuracy_score(labels2, preds2)
    precision2 = precision_score(labels2, preds2)
    recall2 = recall_score(labels2, preds2)
    f12 = f1_score(labels2, preds2)

    print("{:15}{:<.3f}".format('accuracy:', accuracy2))
    print("{:15}{:<.3f}".format('precision:', precision2))
    print("{:15}{:<.3f}".format('recall:', recall2))
    print("{:15}{:<.3f}".format('f1', f12))

    return accuracy2, precision2, recall2, f12


def get_kfold_data(k, i, raw_mohx):
    '''获取k折交叉验证某一折的训练集和验证集
    '''
    # 返回第 i+1 折 (i = 0 -> k-1) 交叉验证时所需要的训练和验证数据，X_train为训练集，X_valid为验证集
    fold_size = len(raw_mohx) // k  # 每份的个数:数据总条数/折数（组数）

    val_start = i * fold_size
    if i != k - 1:
        val_end = (i + 1) * fold_size
        val_raw_mohx = raw_mohx[val_start:val_end]
        tr_raw_mohx = raw_mohx[0:val_start]+raw_mohx[val_end:]
    else:  # 若是最后一折交叉验证
        val_raw_mohx= raw_mohx[val_start:]
        tr_raw_mohx = raw_mohx[0:val_start]

    return tr_raw_mohx, val_raw_mohx


def traink(tr_raw_mohx, val_raw_mohx, TOTAL_EPOCHS, device, bert_base_model_dir, mrbert_model_dir, max_len, batch_size, learning_rate, max_grad_norm, repr, integrate, relmodel, operation):
    '''第k折 模型训练
    '''
    tr_sentences = [r[0] for r in tr_raw_mohx]
    val_sentences = [r[0] for r in val_raw_mohx]

    tr_labels0 = [r[1] for r in tr_raw_mohx]
    val_labels0 = [r[1] for r in val_raw_mohx]

    tr_s_pos = [r[2] for r in tr_raw_mohx]
    val_s_pos = [r[2] for r in val_raw_mohx]

    tr_v_pos = [r[3] for r in tr_raw_mohx]
    val_v_pos = [r[3] for r in val_raw_mohx]

    tr_o_pos = [r[4] for r in tr_raw_mohx]
    val_o_pos = [r[4] for r in val_raw_mohx]

    tr_labels2 = [[r[5]] for r in tr_raw_mohx]
    val_labels2 = [[r[5]] for r in val_raw_mohx]

    tr_embeddings = [[r[6]] for r in tr_raw_mohx]
    val_embeddings = [[r[6]] for r in val_raw_mohx]

    tr_tokenized_texts = insert_tag(tr_sentences, tr_s_pos, tr_v_pos, tr_o_pos)

    val_tokenized_texts = insert_tag(val_sentences, val_s_pos, val_v_pos, val_o_pos)

    tokenizer = BertWordPieceTokenizer('./vocab.txt',lowercase=True)
    tokenizer.add_special_tokens(['[subj]','[/subj]', '[verb]','[/verb]','[obj]','[/obj]'])
    config = BertConfig.from_pretrained('./config.json')
    config.num_labels1 = 2
    config.num_labels2 = 2

    if operation == 'train':
        model = MrBERT.from_pretrained(bert_base_model_dir, config=config, bert_model_dir=bert_base_model_dir, device=device, repr=repr,
                                       integrate=integrate, relmodel=relmodel)
    elif operation == 'finetune':
        model = MrBERT(model_dir=bert_base_model_dir, config=config, device=device, repr=repr, integrate=integrate, relmodel=relmodel)
        checkpoint = torch.load(mrbert_model_dir + '/pytorch_model.bin', map_location=device)
        model.load_state_dict(checkpoint)

    model.to(device)

    # ! get inputs
    tr_input_ids, tr_labels1, tr_labels2, tr_masks, tr_embeddings = get_inputs(tokenizer, tr_tokenized_texts, tr_labels0, tr_labels2, max_len, tr_embeddings)
    val_input_ids, val_labels1, val_labels2, val_masks, val_embeddings = get_inputs(tokenizer, val_tokenized_texts, val_labels0, val_labels2, max_len, val_embeddings)

    train_data = TensorDataset(tr_input_ids, tr_masks, tr_labels1, tr_labels2, tr_embeddings)
    train_loader = DataLoader(train_data,  batch_size=batch_size,shuffle=True)

    val_data = TensorDataset(val_input_ids, val_masks, val_labels1, val_labels2, val_embeddings)
    val_loader = DataLoader(val_data, batch_size=1, shuffle=False)

    # ! 定义 optimizer
    no_decay = ["bias", "LayerNorm.weight"]

    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": 0.01,
        },
        {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], "weight_decay": 0.0},
    ]

    optimizer = AdamW(optimizer_grouped_parameters,lr=learning_rate)

    t_total = len(tr_input_ids)/batch_size*TOTAL_EPOCHS+1
    num_warmup_steps = int(t_total/10)*2
    logging.info('t_total: %d warmup: %d' % (t_total, num_warmup_steps))
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=num_warmup_steps, num_training_steps=t_total)

    val_accs1, val_accs2, val_ps1, val_ps2, val_rs1, val_rs2, val_f1s1, val_f1s2, results1,results2  = [],[],[],[],[],[],[],[],[],[]
    for epoch in range(TOTAL_EPOCHS):
        print('===== Start training: epoch {} ====='.format(epoch + 1))

        model.train()
        tr_loss = 0
        nb_tr_steps = 0

        # ! training
        for step, batch in enumerate(train_loader):
            batch = tuple(t.to(device) for t in batch)
            b_input_ids, b_input_mask, b_labels1, b_labels2, b_embeddings = batch

            outputs1,outputs2 = model(input_ids=b_input_ids, token_type_ids=None,
                            attention_mask=b_input_mask, labels1=b_labels1, labels2=b_labels2, embeddings = b_embeddings)

            loss = outputs1[0] + outputs2[0]

            loss.backward()

            tr_loss += float(loss.item())

            nb_tr_steps += 1

            # ! 减小梯度 https://www.cnblogs.com/lindaxin/p/7998196.html
            torch.nn.utils.clip_grad_norm_(parameters=model.parameters(), max_norm=max_grad_norm)
            # ! 更新参数
            optimizer.step()
            scheduler.step()
            model.zero_grad()

        print("\nEpoch {} of training loss: {}".format(epoch + 1, tr_loss / nb_tr_steps))

        # ! Validation
        model.eval()
        eval_loss, eval_accuracy, eval_precision, eval_recall, eval_f1 = 0, 0, 0, 0, 0
        nb_eval_steps = 0

        preds1, labels1, preds2, labels2 = [], [], [], []
        for step, batch in enumerate(val_loader):
            batch = tuple(t.to(device) for t in batch)
            b_input_ids, b_input_mask, b_labels1, b_labels2, b_embeddings = batch
            with torch.no_grad():
                outputs1, outputs2 = model(input_ids=b_input_ids, token_type_ids=None,
                                           attention_mask=b_input_mask, labels1=b_labels1, labels2=b_labels2, embeddings=b_embeddings)

                tmp_eval_loss1, logits1 = outputs1[:2]
                tmp_eval_loss2, logits2 = outputs2[:2]
                tmp_eval_loss = tmp_eval_loss1 + tmp_eval_loss2

            values1, logits1 = torch.max(F.softmax(logits1, dim=-1), dim=-1)[:2]
            logits2 = logits2.view(-1)
            if logits2 > 0.5:
                logits2 = torch.Tensor([1])
            else:
                logits2 = torch.Tensor([0])

            ture_labels1 = b_labels1[0]
            logits1 = logits1[0]
            ture_labels2 = b_labels2[0]
            # ! detach的方法，将variable参数从网络中隔离开，不参与参数更新
            logits1 = logits1.detach().cpu().numpy()
            logits2 = logits2.detach().cpu().numpy()
            ture_labels1 = ture_labels1.cpu().numpy()
            ture_labels2 = ture_labels2.cpu().numpy()

            preds2.append(logits2)
            labels2.append(ture_labels2)
            nb_eval_steps += 1

            eval_loss += tmp_eval_loss.mean().item()

        # ! 计算评估值

        val_preds2 = np.array(preds2)
        val_labels2 = np.array(labels2)

        # 打印信息
        print("{:15}{:<.3f}".format('val loss:', eval_loss / nb_eval_steps))
        val_accuracy2, val_precision2, val_recall2, val_f12 = evaluation(val_labels2, val_preds2)

        val_accs2.append(val_accuracy2)
        val_ps2.append(val_precision2)
        val_rs2.append(val_recall2)
        val_f1s2.append(val_f12)

    print("===== Train Finished =====\n")
    index=val_f1s2.index(max(val_f1s2))
    print("{:15}{:<}".format("max epoch", index+1))
    print("{:15}{:<.3f}".format("accuracy", val_accs2[index]))
    print("{:15}{:<.3f}".format("precision", val_ps2[index]))
    print("{:15}{:<.3f}".format("recall", val_rs2[index]))
    print("{:15}{:<.3f}".format("f1", val_f1s2[index]))

    return val_accs2[index], val_ps2[index], val_rs2[index], val_f1s2[index]


def k_fold(k, raw_mohx, num_epochs, device, bert_base_model_dir, mrbert_model_dir, max_len, batch_size, lr, max_grad_norm, repr, integrate, relmodel, operation):
    '''k折
    '''
    val_acc_sum, val_p_sum = 0, 0
    val_r_sum, val_f1_sum = 0, 0
    for i in range(k):
        print('*' * 25, '第', i + 1, '折', '*' * 25)
        data = get_kfold_data(k, i, raw_mohx)  # 获取k折交叉验证的训练和验证数据

        # 每份数据进行训练
        val_acc, val_p, val_r, val_f1 = traink( *data, num_epochs, device, bert_base_model_dir, mrbert_model_dir, max_len, batch_size, lr, max_grad_norm, repr, integrate, relmodel, operation)

        val_acc_sum += val_acc
        val_p_sum += val_p
        val_r_sum += val_r
        val_f1_sum += val_f1

    print('\n', '#' * 10, 'mohx 最终十折交叉验证结果', '#' * 10)

    print('average accuracy:{:.3f}, average precision:{:.3f}'.format(val_acc_sum / k, val_p_sum / k))
    print('average recall:{:.3f}, average f1:{:.3f}'.format(val_r_sum / k, val_f1_sum / k))

    return


def test(test_raw_mohx, device, bert_base_model_dir, mrbert_model_dir, max_len, repr, integrate, relmodel):
    '''
    test
    '''
    test_sentences = [r[0] for r in test_raw_mohx]

    test_labels0 = [r[1] for r in test_raw_mohx]

    test_s_pos = [r[2] for r in test_raw_mohx]

    test_v_pos = [r[3] for r in test_raw_mohx]

    test_o_pos = [r[4] for r in test_raw_mohx]

    test_labels2 = [[r[5]] for r in test_raw_mohx]

    test_embeddings = [[r[6]] for r in test_raw_mohx]

    test_tokenized_texts= insert_tag(test_sentences, test_s_pos, test_v_pos, test_o_pos)

    tokenizer = BertWordPieceTokenizer('./vocab.txt',lowercase=True)
    tokenizer.add_special_tokens(['[subj]','[/subj]', '[verb]','[/verb]','[obj]','[/obj]'])
    config = BertConfig.from_pretrained('./config.json')
    config.num_labels1 = 2
    config.num_labels2 = 2

    model = MrBERT(model_dir=bert_base_model_dir, config=config, device=device, repr=repr, integrate=integrate, relmodel=relmodel)
    checkpoint = torch.load(mrbert_model_dir+'/pytorch_model.bin', map_location=device)
    model.load_state_dict(checkpoint)
    # model = MrBERT.from_pretrained(model_dir, model_dir=model_dir, config=config, device=device, repr=repr,
    #                                integrate=integrate, relmodel=relmodel)
    model.to(device)
    model.eval()

    test_input_ids, test_labels1,test_labels2, test_masks, test_embeddings = get_inputs(tokenizer, test_tokenized_texts, test_labels0, test_labels2, max_len, test_embeddings)

    test_data = TensorDataset(test_input_ids, test_masks, test_labels1, test_labels2, test_embeddings)
    test_loader = DataLoader(test_data, batch_size=1, shuffle=False)

    # ! Test
    preds1, labels1, preds2, labels2, t_labels1, p_labels1, t_labels2, p_labels2 = [], [], [], [], [], [], [], []
    for step, batch in enumerate(test_loader):
        batch = tuple(t.to(device) for t in batch)
        b_input_ids, b_input_mask, b_labels1, b_labels2, b_embeddings = batch

        with torch.no_grad():
            outputs1, outputs2 = model(input_ids=b_input_ids, token_type_ids=None,
                                                 attention_mask=b_input_mask, labels1=b_labels1, labels2=b_labels2, embeddings=b_embeddings)
            tmp_eval_loss1, logits1 = outputs1[:2]
            tmp_eval_loss2, logits2 = outputs2[:2]

        values1, logits1 = torch.max(F.softmax(logits1, dim=-1), dim=-1)[:2]

        logits2 = logits2.view(-1)
        if logits2 > 0.5:
            logits2 = torch.Tensor([1])
        else:
            logits2 = torch.Tensor([0])

        ture_labels1 = b_labels1[0]
        logits1 = logits1[0]
        ture_labels2 = b_labels2[0][0]
        logits2 = logits2[0]

        t_labels2.append(ture_labels2)
        p_labels2.append(logits2)

        # ! detach的方法，将variable参数从网络中隔离开，不参与参数更新
        logits1 = logits1.detach().cpu().numpy()
        logits2 = logits2.detach().cpu().numpy()
        ture_labels1 = ture_labels1.cpu().numpy()
        ture_labels2 = ture_labels2.cpu().numpy()

        preds2.append(logits2)
        labels2.append(ture_labels2)

    # ! 计算评估值
    preds2 = np.array(preds2)
    labels2 = np.array(labels2)

    # 打印信息
    print("--- Test ---")
    eval_accuracy2, eval_precision2, eval_recall2, eval_f12 = evaluation(labels2, preds2)


def main():
    """
    ? 1. 设置基本参数
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', default='0', type=str,
                        required=False, help='选择设备')
    parser.add_argument('--seed', default=4, type=int,
                        required=False, help='输入种子数')
    parser.add_argument('--bert_base_model_dir', type=str, required=True, help='bert模型目录')
    parser.add_argument('--mrbert_model_dir',default='./model', type=str, required=False, help='mrbert模型目录')
    parser.add_argument('--max_len', default=30, type=int, required=False, help='句子最大长度')
    parser.add_argument('--kfold', default=10, type=int, required=False, help='k折交叉验证')
    parser.add_argument('--batch_size', default=16, type=int, required=False, help='训练batch_size')
    parser.add_argument('--lr', default=5e-5, type=float, required=False, help='学习率')
    parser.add_argument('--num_epochs', default=10, type=int, required=False, help='训练epoch')
    parser.add_argument('--max_grad_norm', default=1.0, type=float, required=False)
    parser.add_argument('--repr', default='average', type=str, required=False, choices=['tag', 'average'], help='获取表示形式：tag/average')
    parser.add_argument('--integrate', default='average', type=str, required=False, choices=['average', 'maxout', 'concat'], help='整合策略：average/maxout/concat')
    parser.add_argument('--relmodel', default='bilinear', type=str, required=False, choices=['linear', 'bilinear', 'nt'], help='模型：linear/bilinear/nt（neural tensor）')
    parser.add_argument('--operation', default='train', type=str, required=False, choices=['train', 'finetune', 'test'], help='选择操作')
    args = parser.parse_args()
    print('args:\n' + args.__repr__())

    # ? 种子数设置
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)

    # ! 用以保证实验的可重复性，使每次运行的结果完全一致
    torch.backends.cudnn.deterministic = True

    os.environ['CUDA_VISIBLE_DEVICES'] = args.device

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    raw_mohx = load_mohx()

    if args.operation == 'test':
        test(raw_mohx, device, args.bert_base_model_dir, args.mrbert_model_dir, args.max_len, args.repr, args.integrate, args.relmodel)
    else:
        k_fold(args.kfold, raw_mohx, args.num_epochs, device, args.bert_base_model_dir, args.mrbert_model_dir, args.max_len, args.batch_size, args.lr, args.max_grad_norm, args.repr, args.integrate, args.relmodel, args.operation)
if __name__ == "__main__":
    main()

