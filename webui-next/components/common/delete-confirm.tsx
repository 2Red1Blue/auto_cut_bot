"use client";

import { Trash2 } from "lucide-react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

interface DeleteConfirmProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
  title?: string;
  description?: string;
  itemName?: string;
}

export function DeleteConfirm({
  open,
  onOpenChange,
  onConfirm,
  title = "Delete session?",
  description = "This action cannot be undone.",
  itemName,
}: DeleteConfirmProps) {
  const displayTitle = itemName ? `Delete "${itemName}"?` : title;
  const displayDescription = itemName
    ? `Are you sure you want to delete "${itemName}"? This action cannot be undone.`
    : description;

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <div className="mb-3 flex items-center justify-center">
            <div className="grid h-12 w-12 place-items-center rounded-full bg-destructive/10">
              <Trash2 className="h-5 w-5 text-destructive" strokeWidth={2} />
            </div>
          </div>
          <AlertDialogTitle>{displayTitle}</AlertDialogTitle>
          <AlertDialogDescription>{displayDescription}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel onClick={() => onOpenChange(false)}>
            Cancel
          </AlertDialogCancel>
          <AlertDialogAction
            onClick={onConfirm}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            Delete
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}