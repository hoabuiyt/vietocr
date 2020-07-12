import os
import random
from PIL import Image
from collections import defaultdict
import numpy as np
import torch
import lmdb
import six

from torch.utils.data import Dataset
from torch.utils.data.sampler import Sampler
from vietocr.tool.translate import process_image
from vietocr.tool.create_dataset import createDataset

class OCRDataset(Dataset):
    def __init__(self, root_dir, annotation_path, vocab, image_height=32, image_min_width=32, image_max_width=512, transform=None):
        self.root_dir = root_dir
        self.annotation_path = os.path.join(root_dir, annotation_path)
        self.vocab = vocab

        self.image_height = image_height
        self.image_min_width = image_min_width
        self.image_max_width = image_max_width

        self.lmdb_path =  '/tmp/{}'.format(annotation_path)
        createDataset(self.lmdb_path, root_dir, annotation_path)
        
        self.env = lmdb.open(
            self.lmdb_path,
            max_readers=8,
            readonly=True,
            lock=False,
            readahead=False,
            meminit=False)

        with self.env.begin(write=False) as txn:
            nSamples = int(txn.get('num-samples'.encode()))
            self.nSamples = nSamples

#        with open(self.annotation_path, 'r') as ann_file:
#            lines = ann_file.readlines()
#            self.annotations = [l.strip().split('\t') for l in lines]
            
        self.build_cluster_indices()

    def build_cluster_indices(self):
        self.cluster_indices = defaultdict(list)
        
        for i in range(self.__len__()):
            sample = self.__getitem__(i)
            img = sample['img']
            width = img.shape[-1]

            self.cluster_indices[width].append(i)


    def read_data(self, idx):

        with self.env.begin(write=False) as txn:
            img_file = 'image-%09d'%idx
            label_file = 'label-%09d'%idx
            path_file = 'path-%09d'%idx

            imgbuf = txn.get(img_file.encode())
            label = txn.get(label_file.encode()).decode()
            img_path = txn.get(path_file.encode()).decode()

            buf = six.BytesIO()
            buf.write(imgbuf)
            buf.seek(0)
            
            try:
                img = Image.open(buf).convert('RGB')
                img_bw = process_image(img, self.image_height, self.image_min_width, self.image_max_width)
            except IOError:
                print('Corrupted image for %d' % index)

            
        word = self.vocab.encode(label)

        return img_bw, word, img_path

    def __getitem__(self, idx):
        #img_path, lex =  self.annotations[idx]
        img, word, img_path = self.read_data(idx)

        img_path = os.path.join(self.root_dir, img_path)

        sample = {'img': img, 'word': word, 'img_path': img_path}

        return sample

    def __len__(self):
        return self.nSamples

class ClusterRandomSampler(Sampler):
    
    def __init__(self, data_source, batch_size, shuffle=True):
        self.data_source = data_source
        self.batch_size = batch_size
        self.shuffle = shuffle        

    def flatten_list(self, lst):
        return [item for sublist in lst for item in sublist]

    def __iter__(self):
        batch_lists = []
        for cluster, cluster_indices in self.data_source.cluster_indices.items():
            batches = [cluster_indices[i:i + self.batch_size] for i in range(0, len(cluster_indices), self.batch_size)]
            batches = [_ for _ in batches if len(_) == self.batch_size]
            if self.shuffle:
                random.shuffle(batches)

            batch_lists.append(batches)

        lst = self.flatten_list(batch_lists)
        if self.shuffle:
            random.shuffle(lst)

        lst = self.flatten_list(lst)

        return iter(lst)

    def __len__(self):
        return len(self.data_source)

def collate_fn(batch):
    filenames = []
    img = []
    target_weights = []
    tgt_input = []
    max_label_len = max(len(sample['word']) for sample in batch)
    for sample in batch:
        img.append(sample['img'])
        filenames.append(sample['img_path'])
        label = sample['word']
        label_len = len(label)
        
        
        tgt = np.concatenate((
            label,
            np.zeros(max_label_len - label_len, dtype=np.int32)))
        tgt_input.append(tgt)

        one_mask_len = label_len - 1

        target_weights.append(np.concatenate((
            np.ones(one_mask_len, dtype=np.float32),
            np.zeros(max_label_len - one_mask_len,dtype=np.float32))))
        
    img = np.array(img, dtype=np.float32)


    tgt_input = np.array(tgt_input, dtype=np.int64).T
    tgt_output = np.roll(tgt_input, -1, 0).T
    tgt_output[:, -1]=0

    tgt_padding_mask = np.array(target_weights)==0

    rs = {
        'img': torch.FloatTensor(img),
        'tgt_input': torch.LongTensor(tgt_input),
        'tgt_output': torch.LongTensor(tgt_output),
        'tgt_padding_mask':torch.BoolTensor(tgt_padding_mask),
        'filenames': filenames
    }   
    
    return rs
