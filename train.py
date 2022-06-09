import argparse
import os
import numpy as np
import torch
import tqdm
from tensorboardX import SummaryWriter
from torch.cuda.amp import GradScaler, autocast
from torch.utils.data import DataLoader

from dataset.CamVid import CamVid
from loss import DiceLoss
from model.build_BiSeNet import BiSeNet
from utils import poly_lr_scheduler
from utils import reverse_one_hot, compute_global_accuracy, fast_hist, per_class_iu


def val(args, model, dataloader, csv_path):
    print("\n", "=" * 100, sep="")
    print('Start val!')
    # label_info = get_label_info(csv_path)
    with torch.no_grad():
        model.eval()
        precision_record = []
        hist = np.zeros((args.num_classes, args.num_classes))
        for i, (data, label) in enumerate(dataloader):
            if torch.cuda.is_available() and args.use_gpu:
                data = data.cuda()
                label = label.cuda()

            # get RGB predict image
            predict = model(data).squeeze()
            predict = reverse_one_hot(predict)
            predict = np.array(predict.cpu())

            # get RGB label image
            label = label.squeeze()
            if args.loss == 'dice':
                label = reverse_one_hot(label)
            label = np.array(label.cpu())

            # compute per pixel accuracy

            precision = compute_global_accuracy(predict, label)
            hist += fast_hist(label.flatten(), predict.flatten(), args.num_classes)

            precision_record.append(precision)
        precision = np.mean(precision_record)
        # miou = np.mean(per_class_iu(hist))
        miou_list = per_class_iu(hist)[:-1]
        # miou_dict, miou = cal_miou(miou_list, csv_path)
        miou = np.mean(miou_list)
        print(f'precision per pixel for test: {precision:.3f}')
        print(f'mIoU for validation: {miou:.3f}')
        # miou_str = ''
        # for key in miou_dict:
        #     miou_str += '{}:{},\n'.format(key, miou_dict[key])
        # print('mIoU for each class:')
        # print(miou_str)

        print("=" * 100, "\n", sep="")

        return precision, miou


def train(args, model, optimizer, dataloader_train, dataloader_val, csv_path, curr_epoch):
    writer = SummaryWriter(comment=''.format(args.optimizer, args.context_path))
    scaler = GradScaler()

    if args.loss == 'dice':
        loss_func = DiceLoss()
    elif args.loss == 'crossentropy':
        loss_func = torch.nn.CrossEntropyLoss()
    max_miou = 0
    step = 0

    for epoch in range(curr_epoch + 1, args.num_epochs + 1):
        lr = poly_lr_scheduler(optimizer, args.learning_rate, iter=epoch, max_iter=args.num_epochs)
        model.train()

        tq = tqdm.tqdm(total=len(dataloader_train) * args.batch_size)
        tq.set_description(f'epoch {epoch}, lr {lr:.6f}')
        loss_record = []

        for i, (data, label) in enumerate(dataloader_train):
            if torch.cuda.is_available() and args.use_gpu:
                data = data.cuda()
                label = label.cuda()

            with autocast():
                output, output_sup1, output_sup2 = model(data)

                loss1 = loss_func(output, label)
                loss2 = loss_func(output_sup1, label)
                loss3 = loss_func(output_sup2, label)
                loss = loss1 + loss2 + loss3

            tq.update(args.batch_size)
            tq.set_postfix(loss='%.6f' % loss)

            optimizer.zero_grad()
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            step += 1

            writer.add_scalar('loss_step', loss, step)
            loss_record.append(loss.item())

            scaler.update()
        tq.close()

        loss_train_mean = np.mean(loss_record)
        writer.add_scalar('epoch/loss_epoch_train', float(loss_train_mean), epoch)
        print(f'loss for train : {loss_train_mean:.6f}')

        # **** Checkpoint saving ****
        if epoch % args.checkpoint_step == 0 and epoch != 0:
            print("\n", "*" * 100, sep="")
            print("Saving checkpoint...")
            if not os.path.isdir(args.save_model_path):
                os.mkdir(args.save_model_path)

            checkpoint = {
                'epoch': epoch,
                'model_state_dict': model.module.state_dict(),
                'optimizer_state_dict': optimizer.state_dict()
            }

            torch.save(checkpoint,
                       os.path.join(args.save_model_path, 'latest_dice_loss.pth'))
            print("Done!")
            print("*" * 100, "\n", sep="")

        # **** Validation model saving ****
        if epoch % args.validation_step == 0 and epoch != 0:
            precision, miou = val(args, model, dataloader_val, csv_path)
            if miou > max_miou:
                max_miou = miou
                os.makedirs(args.save_model_path, exist_ok=True)
                torch.save(model.module.state_dict(),
                           os.path.join(args.save_model_path, 'best_dice_loss.pth'))
            writer.add_scalar('epoch/precision_val', precision, epoch)
            writer.add_scalar('epoch/miou val', miou, epoch)


def main(params):
    # basic parameters
    parser = argparse.ArgumentParser()
    parser.add_argument('--num_epochs', type=int, default=300, help='Number of epochs to train for')
    parser.add_argument('--epoch_start_i', type=int, default=0, help='Start counting epochs from this number')
    parser.add_argument('--checkpoint_step', type=int, default=100, help='How often to save checkpoints (epochs)')
    parser.add_argument('--validation_step', type=int, default=10, help='How often to perform validation (epochs)')
    parser.add_argument('--dataset', type=str, default="CamVid", help='Dataset you are using.')
    parser.add_argument('--crop_height', type=int, default=720, help='Height of cropped/resized input image to network')
    parser.add_argument('--crop_width', type=int, default=960, help='Width of cropped/resized input image to network')
    parser.add_argument('--batch_size', type=int, default=1, help='Number of images in each batch')
    parser.add_argument('--context_path', type=str, default="resnet101",
                        help='The context path model you are using, resnet18, resnet101.')
    parser.add_argument('--learning_rate', type=float, default=0.01, help='learning rate used for train')
    parser.add_argument('--data', type=str, default='', help='path of training data')
    parser.add_argument('--num_workers', type=int, default=4, help='num of workers')
    parser.add_argument('--num_classes', type=int, default=32, help='num of object classes (with void)')
    parser.add_argument('--cuda', type=str, default='0', help='GPU ids used for training')
    parser.add_argument('--use_gpu', type=bool, default=True, help='whether to user gpu for training')
    parser.add_argument('--pretrained_model_path', type=str, default=None, help='path to pretrained model')
    parser.add_argument('--save_model_path', type=str, default=None, help='path to save model')
    parser.add_argument('--optimizer', type=str, default='rmsprop', help='optimizer, support rmsprop, sgd, adam')
    parser.add_argument('--loss', type=str, default='dice', help='loss function, dice or crossentropy')

    args = parser.parse_args(params)
    print(args)

    # create dataset and dataloader
    train_path = [os.path.join(args.data, 'train'), os.path.join(args.data, 'val')]
    train_label_path = [os.path.join(args.data, 'train_labels'), os.path.join(args.data, 'val_labels')]
    test_path = os.path.join(args.data, 'test')
    test_label_path = os.path.join(args.data, 'test_labels')
    csv_path = os.path.join(args.data, 'class_dict.csv')
    dataset_train = CamVid(train_path,
                           train_label_path,
                           csv_path,
                           scale=(args.crop_height, args.crop_width),
                           loss=args.loss,
                           mode='train')

    dataloader_train = DataLoader(dataset_train,
                                  batch_size=args.batch_size,
                                  shuffle=True,
                                  num_workers=args.num_workers,
                                  drop_last=True)
    dataset_val = CamVid(test_path,
                         test_label_path,
                         csv_path,
                         scale=(args.crop_height, args.crop_width),
                         loss=args.loss,
                         mode='test')

    dataloader_val = DataLoader(dataset_val,
                                # this has to be 1
                                batch_size=1,
                                shuffle=True,
                                num_workers=args.num_workers)

    # build model
    os.environ['CUDA_VISIBLE_DEVICES'] = args.cuda
    model = BiSeNet(args.num_classes, args.context_path)
    if torch.cuda.is_available() and args.use_gpu:
        model = torch.nn.DataParallel(model).cuda()

    # build optimizer
    if args.optimizer == 'rmsprop':
        optimizer = torch.optim.RMSprop(model.parameters(), args.learning_rate)
    elif args.optimizer == 'sgd':
        optimizer = torch.optim.SGD(model.parameters(), args.learning_rate, momentum=0.9, weight_decay=1e-4)
    elif args.optimizer == 'adam':
        optimizer = torch.optim.Adam(model.parameters(), args.learning_rate)
    else:  # rmsprop
        print('not supported optimizer \n')
        return None

    # load pretrained model if exists
    curr_epoch = 0
    if args.pretrained_model_path is not None:
        print("\n", "*" * 100, sep="")
        print(f'Loading model from {args.pretrained_model_path} ...')

        loaded_checkpoint = torch.load(args.pretrained_model_path)
        model.module.load_state_dict(loaded_checkpoint['model_state_dict'])
        optimizer.load_state_dict(loaded_checkpoint['optimizer_state_dict'])
        curr_epoch = loaded_checkpoint['epoch'] + 1

        print(f"\t- epoch done from last checkpoint: {curr_epoch - 1}")
        print('Done!')
        print("*" * 100, "\n", sep="")

    # train
    train(args, model, optimizer, dataloader_train, dataloader_val, csv_path, curr_epoch)

    val(args, model, dataloader_val, csv_path)


if __name__ == '__main__':
    params = [
        '--num_epochs', '100',
        '--learning_rate', '2.5e-2',
        '--data', '../datasets/CamVid/',
        '--num_workers', '8',
        '--num_classes', '12',
        '--cuda', '0',
        '--batch_size', '4',
        '--save_model_path', './checkpoints_18_sgd_100',
        '--context_path', 'resnet18',  # set resnet18 or resnet101, only support resnet18 and resnet101
        '--optimizer', 'sgd',
        # '--pretrained_model_path', './checkpoints_18_sgd_100/latest_dice_loss.pth',
        '--checkpoint_step', '2',
        '--loss', 'dice',
    ]
    main(params)
