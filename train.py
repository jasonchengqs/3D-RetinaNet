
import time
import os
import datetime
import torch
import math
from modules import utils
import torch.utils.data as data_utils
from modules import  AverageMeter
from data import custum_collate
from modules.solver import get_optim
from val import validate

logger = utils.get_logger(__name__)

def train(args, net, train_dataset, val_dataset):
    
    optimizer, scheduler, solver_print_str = get_optim(args, net)

    if args.TENSORBOARD:
        from tensorboardX import SummaryWriter

    source_dir = args.SAVE_ROOT+'/source/' # where to save the source
    utils.copy_source(source_dir)

    args.START_ITERATION = 0
    if args.RESUME>100:
        args.START_ITERATION = args.resume
        args.iteration = args.START_ITERATION
        for _ in range(args.iteration-1):
            scheduler.step()
        model_file_name = '{:s}/model_{:06d}.pth'.format(args.SAVE_ROOT, args.START_ITERATION)
        optimizer_file_name = '{:s}/optimizer_{:06d}.pth'.format(args.SAVE_ROOT, args.START_ITERATION)
        net.load_state_dict(torch.load(model_file_name))
        optimizer.load_state_dict(torch.load(optimizer_file_name))
        
    # anchors = anchors.cuda(0, non_blocking=True)
    if args.TENSORBOARD:
        log_dir = '{:s}/tboard-{}-{date:%m-%d-%Hx}'.format(args.log_dir, args.MODE, date=datetime.datetime.now())
        sw = SummaryWriter(log_dir)
    
    logger.info('EXPERIMENT NAME:: ' + args.exp_name)

    for arg in sorted(vars(args)):
        logger.info(str(arg)+': '+str(getattr(args, arg)))
    logger.info(str(net))
    logger.info(solver_print_str)
    net.train()
    # loss counters
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    loc_losses = AverageMeter()
    cls_losses = AverageMeter()

    logger.info(train_dataset.print_str)
    logger.info(val_dataset.print_str)
    epoch_size = len(train_dataset) // args.BATCH_SIZE
    logger.info('Training FPN with {} + {} as backbone '.format(args.ARCH, args.MODEL_TYPE))


    train_data_loader = data_utils.DataLoader(train_dataset, args.BATCH_SIZE, num_workers=args.NUM_WORKERS,
                                  shuffle=True, pin_memory=True, collate_fn=custum_collate, drop_last=True)
    
    val_data_loader = data_utils.DataLoader(val_dataset, args.BATCH_SIZE, num_workers=args.NUM_WORKERS,
                                            shuffle=False, pin_memory=True, collate_fn=custum_collate)
  
    torch.cuda.synchronize()
    start = time.perf_counter()
    iteration = args.START_ITERATION
    total_epochs = math.ceil(args.MAX_ITERS /  epoch_size)
    num_bpe = len(train_data_loader)
    while iteration <= args.MAX_ITERS:
        for i, (images, gt_boxes, gt_labels, ego_labels, counts, img_indexs, wh) in enumerate(train_data_loader):
            if iteration > args.MAX_ITERS:
                break
            iteration += 1
            epoch = int(iteration/num_bpe)
            images = images.cuda(0, non_blocking=True)
            gt_boxes = gt_boxes.cuda(0, non_blocking=True)
            gt_labels = gt_labels.cuda(0, non_blocking=True)
            counts = counts.cuda(0, non_blocking=True)
            ego_labels = ego_labels.cuda(0, non_blocking=True)
            # forward
            torch.cuda.synchronize()
            data_time.update(time.perf_counter() - start)

            # print(images.size(), anchors.size())
            optimizer.zero_grad()
            # pdb.set_trace()
            loss_l, loss_c = net(images, gt_boxes, gt_labels, ego_labels, counts, img_indexs)
            loss_l, loss_c = loss_l.mean(), loss_c.mean()
            loss = loss_l + loss_c

            loss.backward()
            optimizer.step()
            scheduler.step()

            loc_loss = loss_l.item()
            conf_loss = loss_c.item()
            if loc_loss>300:
                lline = '\n\n\n We got faulty LOCATION loss {} {} \n\n\n'.format(loc_loss, conf_loss)
                logger.info(lline)
                loc_loss = 20.0
            if conf_loss>300:
                lline = '\n\n\n We got faulty CLASSIFICATION loss {} {} \n\n\n'.format(loc_loss, conf_loss)
                logger.info(lline)
                conf_loss = 20.0
            
            loc_losses.update(loc_loss)
            cls_losses.update(conf_loss)
            losses.update((loc_loss + conf_loss)/2.0)

            torch.cuda.synchronize()
            batch_time.update(time.perf_counter() - start)
            start = time.perf_counter()

            if iteration % args.LOG_STEP == 0 and iteration > args.LOG_START:
                if args.TENSORBOARD:
                    sw.add_scalars('Classification', {'val': cls_losses.val, 'avg':cls_losses.avg},iteration)
                    sw.add_scalars('Localisation', {'val': loc_losses.val, 'avg':loc_losses.avg},iteration)
                    sw.add_scalars('Overall', {'val': losses.val, 'avg':losses.avg},iteration)
                epoch = iteration // epoch_size
                print_line = 'Itration [{:d}/{:d}]{:06d}/{:06d} loc-loss {:.2f}({:.2f}) cls-loss {:.2f}({:.2f}) ' \
                             'average-loss {:.2f}({:.2f}) DataTime{:0.2f}({:0.2f}) Timer {:0.2f}({:0.2f})'.format( epoch, total_epochs, iteration, args.MAX_ITERS, loc_losses.val, loc_losses.avg, cls_losses.val,
                              cls_losses.avg, losses.val, losses.avg, 10*data_time.val, 10*data_time.avg, 10*batch_time.val, 10*batch_time.avg)

                logger.info(print_line)
                if iteration % (args.LOG_STEP*10) == 0:
                    logger.info(args.exp_name)


            if (iteration % args.VAL_STEP == 0 or iteration== args.INTIAL_VAL or iteration == args.MAX_ITERS) and iteration>0:
                torch.cuda.synchronize()
                tvs = time.perf_counter()
                logger.info('Saving state, iter:' + str(iteration))
                torch.save(net.state_dict(), '{:s}/model_{:06d}.pth'.format(args.SAVE_ROOT, iteration))
                torch.save(optimizer.state_dict(), '{:s}/optimizer_{:06d}.pth'.format(args.SAVE_ROOT, iteration))
                
                net.eval() # switch net to evaluation mode
                
                mAP, ap_all, ap_strs = validate(args, net, val_data_loader, val_dataset, iteration)
                label_types = args.label_types + ['ego_action']
                all_classes = args.all_classes + [args.ego_classes]
                for nlt in range(args.num_label_types+1):
                    for ap_str in ap_strs[nlt]:
                        logger.info(ap_str)
                    ptr_str = '\n{:s} MEANAP:::=> {:0.5f}'.format(label_types[nlt], mAP[nlt])
                    logger.info(ptr_str)

                    if args.TENSORBOARD:
                        sw.add_scalar('{:s}mAP'.format(label_types[nlt]), mAP[nlt], iteration)
                        class_AP_group = dict()
                        for c, ap in enumerate(ap_all[nlt]):
                            class_AP_group[all_classes[nlt][c]] = ap
                        sw.add_scalars('ClassAP-{:s}'.format(label_types[nlt]), class_AP_group, iteration)

                torch.cuda.synchronize()
                t0 = time.perf_counter()
                prt_str = '\nValidation TIME::: {:0.3f}\n\n'.format(t0-tvs)
                logger.info(ptr_str)

                net.train()
                if args.FBN:
                    if args.MULTI_GPUS:
                        net.module.backbone.apply(utils.set_bn_eval)
                    else:
                        net.backbone.apply(utils.set_bn_eval)

                
