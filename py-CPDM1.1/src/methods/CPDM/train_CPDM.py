import math
import torch
import os
import time
from tqdm.auto import tqdm
from torch.autograd import Variable
from torch.nn import functional as F

import src.utilities.utils as utils


TRAINING_DONE_TOKEN = 'CLASSIFIER.TRAINING.DONE'


def train_model(args, model, criterion, optimizer, lr, dsets, batch_size, dset_sizes, use_cuda, num_epochs,
                task_counter,exp_dir='./',
                resume='', saving_freq=5,device=None,combine_label_list=None,gen_dset=None, 
                test_ds_path=[]):
    dset_loaders = {x: torch.utils.data.DataLoader(dsets[x], batch_size=batch_size, num_workers=4,
                                                    shuffle=True, pin_memory=True, persistent_workers=True)
                        for x in ['train', 'val']}
    if gen_dset is not None:
        gen_dset_loaders = {x: torch.utils.data.DataLoader(gen_dset[x], batch_size=batch_size, num_workers=4,
                                                            shuffle=True, pin_memory=True, persistent_workers=True)
                            for x in ['train', 'val']}
    else:
        gen_dset_loaders = None
    this_task_class_to_idx = {combine_label_list[i]: i for i in range(len(combine_label_list))}
    print('dictionary length' + str(len(dset_loaders)))
    since = time.time()
    mem_snapshotted = False
    val_beat_counts = 0
    best_acc = 0.0
    if os.path.isfile(resume):
        print("=> loading checkpoint '{}'".format(resume))
        checkpoint = torch.load(resume)
        start_epoch = checkpoint['epoch']
        best_acc = checkpoint['best_acc']

        model.load_state_dict(checkpoint['state_dict'])
        print('load')
        optimizer.load_state_dict(checkpoint['optimizer'])
        lr = checkpoint['lr']
        print("lr is ", lr)
        val_beat_counts = checkpoint['val_beat_counts']

        print("=> loaded checkpoint '{}' (epoch {})"
              .format(resume, checkpoint['epoch']))
        if os.path.exists(os.path.join(exp_dir, TRAINING_DONE_TOKEN)):
            return model, best_acc
    else:
        start_epoch = 0
        print("=> no checkpoint found at '{}'".format(resume))

    print(str(start_epoch))
    print("lr is", lr)

    for epoch in range(start_epoch, num_epochs):
        print('Epoch {}/{}'.format(epoch, num_epochs - 1))
        print('-' * 10)
        print('task_counter: '+str(task_counter))

        for phase in ['train', 'val']:

            if phase == 'train':
                optimizer, lr, continue_training = set_lr(args, optimizer, lr, count=val_beat_counts)
                if not continue_training:
                    traminate_protocol(since, best_acc)
                    if not os.path.exists(os.path.join(exp_dir, TRAINING_DONE_TOKEN)):
                        torch.save('', os.path.join(exp_dir, TRAINING_DONE_TOKEN))
                    return model, best_acc
                model.train(True)
            else:
                model.train(False)

            running_loss = 0.0
            running_corrects = 0
            running_counter = 0
            if phase == 'test':
                ziploaders = enumerate(dset_loaders[phase])
            else:
                if gen_dset_loaders is not None:
                    ziploaders = enumerate(zip(dset_loaders[phase],gen_dset_loaders[phase]))
                else:
                    ziploaders = enumerate(dset_loaders[phase])

            for _,data in tqdm(ziploaders, desc=f'{phase} epoch {epoch + 1} (lr = {optimizer.param_groups[0]["lr"]})'):

                if (gen_dset_loaders is not None) and (phase != 'test'):
                    inputs, labels = data[0]
                    gen_inputs, gen_labels = data[1]
                    inputs = torch.cat((inputs,gen_inputs))
                    labels = torch.cat((labels,gen_labels))
                else:
                    inputs, labels = data
                if 'mnist' in args.ds_name:
                    inputs = inputs.squeeze()
                if args.class_incremental or args.class_incremental_repetition:
                    l = [this_task_class_to_idx[labels[i].item()] for i in range(len(labels))]
                    ll = torch.tensor(l).reshape(labels.shape)
                    labels = ll

                if use_cuda:
                    inputs, labels = Variable(inputs.cuda(non_blocking=False)), \
                                     Variable(labels.cuda(non_blocking=False))
                else:
                    inputs, labels = Variable(inputs), Variable(labels)
                running_counter+=inputs.shape[0]
                optimizer.zero_grad()

                if args.class_incremental or args.class_incremental_repetition:
                    logits = model(inputs,combine_label_list)
                else:
                    logits = model(inputs)
                _, preds = torch.max(logits.data, 1)

                loss = criterion(logits, labels)


                if phase == 'train':

                    loss.backward()

                    optimizer.step()

                if not mem_snapshotted:
                    utils.save_cuda_mem_req(exp_dir)
                    mem_snapshotted = True

                running_loss += loss.data.item()
                running_corrects += torch.sum(preds == labels.data).item()

            epoch_loss = running_loss / running_counter
            epoch_acc = running_corrects / running_counter

            print('{} Loss: {:.4f} Acc: {:.4f}'.format(
                phase, epoch_loss, epoch_acc))
            if epoch_loss > 1e4 or math.isnan(epoch_loss):
                if not os.path.exists(os.path.join(exp_dir, TRAINING_DONE_TOKEN)):
                    torch.save('', os.path.join(exp_dir, TRAINING_DONE_TOKEN))
                return model, best_acc

            if phase == 'val':
                if epoch_acc > best_acc:
                    del logits, labels, inputs, loss, preds
                    best_acc = epoch_acc
                    torch.save(model, os.path.join(exp_dir, 'best_model.pth.tar'))
                    val_beat_counts = 0
                else:
                    val_beat_counts += 1
        if epoch % saving_freq == 0:

            epoch_file_name = exp_dir + '/' + 'epoch' + '.pth.tar'
            save_checkpoint({
                'epoch': epoch + 1,
                'lr': lr,
                'val_beat_counts': val_beat_counts,
                'epoch_acc': epoch_acc,
                'best_acc': best_acc,
                'arch': 'alexnet',
                'model': model,
                'state_dict': model.state_dict(),
                'optimizer': optimizer.state_dict(),
            }, epoch_file_name)
        print()

    time_elapsed = time.time() - since
    print('Training complete in {:.0f}m {:.0f}s'.format(
        time_elapsed // 60, time_elapsed % 60))
    print('Best val Acc: {:4f}'.format(best_acc))
    if not os.path.exists(os.path.join(exp_dir, TRAINING_DONE_TOKEN)):
        torch.save('', os.path.join(exp_dir, TRAINING_DONE_TOKEN))
    return model, best_acc

def set_lr(args, optimizer, lr, count):

    continue_training = True
    if count > 10:
        continue_training = False
        print("training terminated")
    if count == 5:
        lr = lr * 0.1
        print('lr is set to {}'.format(lr))
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

    return optimizer, lr, continue_training


def traminate_protocol(since, best_acc):
    time_elapsed = time.time() - since
    print('Training complete in {:.0f}m {:.0f}s'.format(
        time_elapsed // 60, time_elapsed % 60))
    print('Best val Acc: {:4f}'.format(best_acc))


def save_checkpoint(state, filename='checkpoint.pth.tar'):
    torch.save(state, filename)


def update_input(self, input, output):
    self.input = input[0].data
    self.output = output


def do_find_most_confidence(args, model, train_dset, batch_size, use_cuda, combine_label_list, current_label_list):
    train_loader = torch.utils.data.DataLoader(train_dset, batch_size=batch_size, num_workers=16,
                                                    shuffle=True, pin_memory=True, persistent_workers=True)
    this_task_class_to_idx = {combine_label_list[i]: i for i in range(len(combine_label_list))}
    result = dict()
    best = dict()
    for label in current_label_list:
        best[label] = 0.0
    model.train(False)
    with torch.no_grad():
        for data in tqdm(train_loader, desc='find most confidence'):
            inputs, _labels = data
            if 'mnist' in args.ds_name:
                inputs = inputs.squeeze()
            if args.class_incremental or args.class_incremental_repetition:
                l = [this_task_class_to_idx[_labels[i].item()] for i in range(len(_labels))]
                ll = torch.tensor(l).reshape(_labels.shape)
                labels = ll

            if use_cuda:
                inputs, labels = Variable(inputs.cuda(non_blocking=True)), \
                                    Variable(labels.cuda(non_blocking=True))
            else:
                inputs, labels = Variable(inputs), Variable(labels)

            if args.class_incremental or args.class_incremental_repetition:
                logits = model(inputs,combine_label_list)
            else:
                logits = model(inputs)
            probs = torch.softmax(logits, dim=1)
            for input, label, _label, prob in zip(inputs, labels, _labels, probs):
                value = prob[label].item()
                if value > best[_label.item()]:
                    best[_label.item()] = value
                    result[_label.item()] = F.interpolate(input.cpu().unsqueeze(dim=0), size=args.image_size)[0]
    return result


def do_find_less_confidence(args, model, train_dset, batch_size, use_cuda, combine_label_list, current_label_list):
    train_loader = torch.utils.data.DataLoader(train_dset, batch_size=batch_size, num_workers=16,
                                                    shuffle=True, pin_memory=True, persistent_workers=True)
    this_task_class_to_idx = {combine_label_list[i]: i for i in range(len(combine_label_list))}
    result = dict()
    best = dict()
    for label in current_label_list:
        best[label] = 2.0
    model.train(False)
    with torch.no_grad():
        for data in tqdm(train_loader, desc='find most confidence'):
            inputs, labels = data
            if 'mnist' in args.ds_name:
                inputs = inputs.squeeze()
            if args.class_incremental or args.class_incremental_repetition:
                l = [this_task_class_to_idx[labels[i].item()] for i in range(len(labels))]
                ll = torch.tensor(l).reshape(labels.shape)
                labels = ll

            if use_cuda:
                inputs, labels = Variable(inputs.cuda(non_blocking=True)), \
                                    Variable(labels.cuda(non_blocking=True))
            else:
                inputs, labels = Variable(inputs), Variable(labels)

            if args.class_incremental or args.class_incremental_repetition:
                logits = model(inputs,combine_label_list)
            else:
                logits = model(inputs)
            probs = torch.softmax(logits, dim=1)
            for input, label, prob in zip(inputs, labels, probs):
                value = prob[label].item()
                if value < best[label.item()]:
                    best[label.item()] = value
                    result[label.item()] = F.interpolate(input.cpu().unsqueeze(dim=0), size=args.image_size)[0]
    return result


def do_find_avg(args, model, train_dset, batch_size, use_cuda, combine_label_list, current_label_list):
    train_loader = torch.utils.data.DataLoader(train_dset, batch_size=batch_size, num_workers=16,
                                                    shuffle=True, pin_memory=True, persistent_workers=True)
    this_task_class_to_idx = {combine_label_list[i]: i for i in range(len(combine_label_list))}
    result = dict()
    count = dict()
    for label in current_label_list:
        count[label] = 0
    model.train(False)
    with torch.no_grad():
        for data in tqdm(train_loader, desc='find most confidence'):
            inputs, labels = data
            if 'mnist' in args.ds_name:
                inputs = inputs.squeeze()
            if args.class_incremental or args.class_incremental_repetition:
                l = [this_task_class_to_idx[labels[i].item()] for i in range(len(labels))]
                ll = torch.tensor(l).reshape(labels.shape)
                labels = ll
            inputs, labels = Variable(inputs), Variable(labels)
            for input, label in zip(inputs, labels):
                if label.item() in result.keys():
                    result[label.item()] += F.interpolate(input.cpu().unsqueeze(dim=0), size=args.image_size)[0]
                else:
                    result[label.item()] = F.interpolate(input.cpu().unsqueeze(dim=0), size=args.image_size)[0]
                count[label.item()] += 1
    for label in current_label_list:
        result[label] = result[label] / count[label]
    return result


def do_random(args, current_label_list):
    result = dict()
    for label in current_label_list:
        result[label] = torch.randn(size=(3, args.image_size, args.image_size), dtype=torch.float32)
    return result
